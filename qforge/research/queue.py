# -*- coding: utf-8 -*-
"""Research Queue: deterministic research agenda driven by registries.

Queue items are generated from state (factors without stats, strategies without
evaluations), so the lab always has a principled next step instead of
ad-hoc parameter fishing.
"""
from __future__ import annotations

import uuid

from .. import db
from ..backtest.engine import BarSource
from ..config import get_settings
from ..data import calendar as cal
from ..data.quality import readiness
from ..factors import REGISTRY as FACTORS
from ..factors import sync_registry as sync_factor_registry
from ..research import evaluator, oos
from ..research.metrics import factor_stats, quantile_spread, rank_ic_series
from ..strategies import REGISTRY as STRATS
from ..strategies import sync_registry as sync_strategy_registry
from ..util import get_logger, now_ts

log = get_logger("qforge.queue")


def seed_queue() -> list[str]:
    """Create PENDING items for anything not yet analyzed/evaluated."""
    sync_factor_registry()
    sync_strategy_registry()
    lab = db.connect_lab()
    try:
        items = []
        have_fs = {r["factor_id"] for r in db.q(lab, "SELECT DISTINCT factor_id FROM factor_stats")}
        for fid in FACTORS:
            if fid not in have_fs:
                items.append(_mk(lab, "factor", fid, priority=3))
        evaluated = {r["strategy_id"] for r in db.q(lab, "SELECT DISTINCT strategy_id FROM evaluations")}
        for sid in STRATS:
            if sid not in evaluated:
                items.append(_mk(lab, "strategy", sid, priority=2))
        return [i["item_id"] for i in items]
    finally:
        lab.close()


def _mk(lab, kind: str, target: str, priority: int = 5) -> dict:
    row = {"item_id": uuid.uuid4().hex[:12], "kind": kind, "target": target,
           "priority": priority, "status": "PENDING", "reason": "auto-seeded",
           "created_at": now_ts(), "updated_at": now_ts(), "result_json": None}
    db.upsert(lab, "research_queue", [row],
              ["item_id", "kind", "target", "priority", "status", "reason",
               "created_at", "updated_at", "result_json"])
    return row


def pending_items(limit: int = 50) -> list[dict]:
    lab = db.connect_lab()
    try:
        rows = db.q(lab, "SELECT * FROM research_queue WHERE status='PENDING' "
                          "ORDER BY priority, created_at LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        lab.close()


def process_queue(max_items: int = 10) -> list[dict]:
    """Run pending research items. Strategy items run with subprocess isolation
    (see runtime.jobs) when invoked through the pipeline; direct runs are inline."""
    results = []
    stock_bars = None
    factor_data = None
    factor_pit = None
    try:
        for item in pending_items(max_items)[:max_items]:
            kind, target = item["kind"], item["target"]
            lab = db.connect_lab()
            try:
                lab.execute("UPDATE research_queue SET status='RUNNING', updated_at=? WHERE item_id=?",
                            (now_ts(), item["item_id"]))
            finally:
                lab.close()
            try:
                if kind == "factor":
                    if factor_pit is None:
                        from ..data.pit import PITReader
                        factor_pit = PITReader(oos.research_clip_end(), strict=True)
                    if factor_data is None:
                        from ..factors import FactorData
                        codes = [r["ts_code"] for r in _research_codes()]
                        factor_data = FactorData(factor_pit, codes, get_start(), oos.research_clip_end())
                    res = analyze_factor(target, data=factor_data, pit=factor_pit)
                elif kind == "strategy":
                    rdy = readiness(target)
                    if not rdy["ready"]:
                        res = {"skipped": "readiness", "detail": rdy}
                    else:
                        strat_cls = STRATS[target]
                        target_pool = getattr(strat_cls, "target_pool", None)
                        if target_pool is None:
                            if stock_bars is None:
                                clip_end = oos.research_clip_end()
                                s = get_settings()
                                data_start = cal.open_days()[0] if cal.open_days() else s.history_start
                                start = max(s.history_start, data_start)
                                stock_bars = BarSource(end=clip_end, tier="production",
                                                       max_names=s.max_universe_names, start=start)
                            res = evaluator.evaluate_strategy(target, bars=stock_bars)
                        else:
                            res = evaluator.evaluate_strategy(target)
                else:
                    res = {"skipped": f"unknown kind {kind}"}
                status = "DONE"
            except Exception as e:
                log.exception("queue item %s/%s failed", kind, target)
                res, status = {"error": str(e)[:300]}, "FAILED"
            lab = db.connect_lab()
            try:
                lab.execute("UPDATE research_queue SET status=?, result_json=?, updated_at=? "
                            "WHERE item_id=?",
                            (status, db.json_dumps(res)[:4000], now_ts(), item["item_id"]))
            finally:
                lab.close()
            results.append({"item": item, "result": res, "status": status})
    finally:
        if factor_pit is not None:
            factor_pit.close()
    return results


# ------------------------------------------------------------------ factor analysis ----
def analyze_factor(factor_id: str, data=None, pit=None) -> dict:
    """RankIC/分层/单调性 within the research window (OOS strictly excluded)."""
    from ..factors import FactorData
    from ..data.pit import PITReader
    clip_end = oos.research_clip_end()
    s_id = factor_id
    own_pit = False
    if pit is None:
        pit = PITReader(clip_end, strict=True)
        own_pit = True
    try:
        if data is None:
            codes = [r["ts_code"] for r in _research_codes()]
            if not codes:
                return {"error": "empty research universe"}
            data = FactorData(pit, codes, get_start(), clip_end)
        codes = getattr(data, "codes", None)
        feats = pit.features(s_id, start=get_start(), end=clip_end, codes=codes)
        if feats.empty:
            return {"error": "no features stored (run features job first)"}
        wide = feats.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last")
        fwd1 = data.adj_close.pct_change(fill_method=None).shift(-1)
        fwd5 = data.adj_close.pct_change(5, fill_method=None).shift(-5)
        fwd21 = data.adj_close.pct_change(21, fill_method=None).shift(-21)
        common_idx = wide.index.intersection(data.adj_close.index)
        wide = wide.reindex(common_idx)
        ic1 = rank_ic_series(wide, fwd1.reindex(common_idx))
        ic5 = rank_ic_series(wide, fwd5.reindex(common_idx))
        ic21 = rank_ic_series(wide, fwd21.reindex(common_idx))
        qs = quantile_spread(wide, fwd21.reindex(common_idx), 5)
        st = factor_stats(ic21, long_short_ann=qs.get("long_short_ann"))
        st.update({"ic5_mean": float(ic5.mean()) if not ic5.empty else None,
                   "ic1_mean": float(ic1.mean()) if not ic1.empty else None,
                   "quantiles": qs})
        lab = db.connect_lab()
        try:
            rows_out = [{"factor_id": s_id, "scope": "research", "window_start": str(common_idx[0]),
                         "window_end": str(common_idx[-1]),
                         "ic_mean": st.get("ic_mean"), "ic_ir": st.get("ic_ir"),
                         "rank_ic_mean": st.get("rank_ic_mean"), "rank_ic_ir": st.get("ic_ir"),
                         "ic_tstat": st.get("ic_tstat"), "turnover": None,
                         "long_short_ann": st.get("long_short_ann"),
                         "monotonics": int(qs.get("monotonic", False)),
                         "computed_at": now_ts(), "stats_json": db.json_dumps(st)[:8000]}]
            db.upsert(lab, "factor_stats", rows_out,
                      ["factor_id", "scope", "window_start", "window_end", "ic_mean", "ic_ir",
                       "rank_ic_mean", "rank_ic_ir", "ic_tstat", "turnover", "long_short_ann",
                       "monotonics", "computed_at", "stats_json"])
            # daily IC series for the dashboard
            with db.tx(lab):
                lab.execute("DELETE FROM factor_ic_daily WHERE factor_id=? AND scope='research'", (s_id,))
                lab.executemany("INSERT OR REPLACE INTO factor_ic_daily VALUES (?,?,?,?)",
                                [(s_id, "research", str(d), float(v)) for d, v in ic1.items()])
        finally:
            lab.close()
        return st
    finally:
        if own_pit and pit is not None:
            pit.close()


def get_start() -> str:
    s = get_settings()
    return s.history_start


def _research_codes() -> list[dict]:
    mk = db.connect_market()
    try:
        rows = db.q(mk, "SELECT ts_code FROM sec_master WHERE sec_type='STOCK'")
        return [dict(r) for r in rows]
    finally:
        mk.close()
