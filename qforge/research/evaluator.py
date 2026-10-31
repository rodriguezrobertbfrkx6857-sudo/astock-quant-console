# -*- coding: utf-8 -*-
"""Strategy evaluation pipeline: backtests, robustness gates, rating rubric.

Priority order (hard-coded): robustness > OOS > risk > return.
Stages: REJECT / RESEARCH / PAPER / CANDIDATE.

All research backtests are clipped before the LOCKED OOS window via
oos.assert_not_oos; OOS is spent only through consume_oos (once, permanently).
"""
from __future__ import annotations

import uuid

import numpy as np
import pandas as pd
from src.quantcore.contracts.safety import SafetyViolation

from .. import db
from ..backtest.engine import BacktestEngine, EngineConfig, BarSource
from ..config import get_settings
from ..data import calendar as cal
from ..data.quality import readiness
from ..research import metrics as M
from ..research import oos
from ..strategies import REGISTRY as STRATS
from ..util import get_logger, now_ts, sha1

log = get_logger("qforge.eval")


# ------------------------------------------------------------------ run one ----
def run_backtest(strategy_id: str, params: dict, start: str, end: str,
                 scope: str = "research", label: str = "",
                 consume_oos: bool = False, persist: bool = True,
                 max_names: int | None = None,
                 bars: BarSource | None = None) -> tuple[str, "BacktestResult"]:
    _legacy_research_disabled()
    s = get_settings()
    uses_oos = 0
    if scope != "oos" and not consume_oos:
        oos.assert_not_oos(start, end, what=f"backtest {strategy_id}")
    elif consume_oos:
        oos.acquire(strategy_id, start, end, consumed_by=label or "explicit")
        uses_oos = 1
    elif oos.status_for_window(start, end, scope="GLOBAL") == "KNOWN":
        uses_oos = 1  # known (spent) window: allowed, flagged
    strat_cls = STRATS[strategy_id]
    engine = BacktestEngine(strat_cls(), params, start, end,
                            max_names=max_names or s.max_universe_names, label=label,
                            scope=scope, bars=bars)
    result = engine.run()
    bt_id = uuid.uuid4().hex[:16]
    if persist:
        lab = db.connect_lab()
        try:
            db.upsert(lab, "backtests", [{
                "bt_id": bt_id, "strategy_id": strategy_id,
                "params_hash": sha1(params)[:10], "params_json": db.json_dumps(params),
                "start": start, "end": end, "scope": scope,
                "created_at": now_ts(), "engine": "qforge.core",
                "engine_version": _engine_version(), "uses_oos": uses_oos,
                "seed": None, "note": label,
            }], ["bt_id", "strategy_id", "params_hash", "params_json", "start", "end",
                 "scope", "created_at", "engine", "engine_version", "uses_oos", "seed", "note"])
            nav_rows = [{"bt_id": bt_id, "trade_date": d, "nav": float(r.nav),
                         "ret": float(r.ret), "bench_nav": float(r.bench_nav),
                         "bench_ret": float(r.bench_ret), "exposure": float(r.exposure)}
                        for d, r in result.nav.iterrows()]
            with db.tx(lab):
                lab.execute("DELETE FROM bt_nav WHERE bt_id=?", (bt_id,))
                lab.executemany("INSERT OR REPLACE INTO bt_nav VALUES (?,?,?,?,?,?,?)",
                                [(r["bt_id"], r["trade_date"], r["nav"], r["ret"],
                                  r["bench_nav"], r["bench_ret"], r["exposure"]) for r in nav_rows])
            fills = result.fills_df
            trade_rows = []
            if not fills.empty:
                for i, r in enumerate(fills.itertuples(index=False)):
                    trade_rows.append((bt_id, i, r.trade_date, r.ts_code, r.side,
                                       float(r.shares), float(r.price),
                                       float(r.shares * r.price), float(r.fee),
                                       float(r.cash_after), r.note))
                with db.tx(lab):
                    lab.execute("DELETE FROM bt_trades WHERE bt_id=?", (bt_id,))
                    lab.executemany(
                        "INSERT OR REPLACE INTO bt_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        trade_rows)
            order_rows = [(bt_id, o.seq, o.issued_date, o.exec_date, o.ts_code, o.side,
                           o.shares, o.target_weight, o.value_delta, o.status, o.age, o.note)
                          for o in engine.orders]
            if order_rows:
                with db.tx(lab):
                    lab.execute("DELETE FROM bt_orders WHERE bt_id=?", (bt_id,))
                    lab.executemany(
                        "INSERT OR REPLACE INTO bt_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        order_rows)
        finally:
            lab.close()
    return bt_id, result


def _engine_version() -> str:
    from .. import __version__
    return __version__


# ------------------------------------------------------------------ gates ----
def _rubric(stats: dict, folds: dict, sens: dict, boot: dict) -> tuple[str, dict]:
    """robustness > OOS > risk > return."""
    gates = {}
    sharpe = stats.get("sharpe") or 0.0
    mdd = stats.get("mdd") or 0.0
    cost = stats.get("cost_drag_ann") or 0.0
    turnover = stats.get("turnover_ann") or 0.0
    gates["sharpe"] = {"value": round(sharpe, 3), "pass": bool(sharpe >= 0.8)}
    gates["mdd"] = {"value": round(mdd, 3), "pass": bool(mdd >= -0.35)}
    gates["folds_positive"] = {"value": folds.get("positive_ratio"),
                               "pass": bool((folds.get("positive_ratio") or 0) >= 0.6)}
    sens_ratio = sens.get("min_neighbor_sharpe")
    gates["param_stability"] = {
        "value": None if sens_ratio is None else round(sens_ratio, 3),
        "pass": bool(sens.get("all_positive") and (sens_ratio or 0) > 0.3)}
    gates["bootstrap_ci_lo"] = {"value": None if boot.get("lo") is None else round(boot["lo"], 3),
                                "pass": bool(boot.get("lo") is not None and boot["lo"] > 0)}
    gates["cost"] = {"value": round(cost, 4), "pass": bool(cost <= 0.03)}
    gates["turnover"] = {"value": round(turnover, 2), "pass": bool(turnover <= 12.0)}
    hard_fail = (sharpe < 0.5) or (mdd < -0.45)
    passes = sum(1 for g in gates.values() if g["pass"])
    if hard_fail or passes <= 3:
        verdict = "REJECT"
    elif passes == len(gates):
        verdict = "PAPER"
    else:
        verdict = "RESEARCH"
    return verdict, gates


# ------------------------------------------------------------------ folds ----
def _walk_forward(strategy_id: str, params: dict, start: str, end: str,
                  folds: int, bars: BarSource | None = None) -> dict:
    od = cal.open_days_between(start, end)
    if len(od) < folds * 120:
        return {"positive_ratio": None, "fold_sharpes": [], "detail": "window too short"}
    seg = np.array_split(od, folds)
    sharpes = []
    for i, test_days in enumerate(seg):
        if len(test_days) < 60:
            continue
        try:
            _, res = run_backtest(strategy_id, params, test_days[0], test_days[-1],
                                  scope=f"fold{i}", label=f"wf-fold{i}", persist=False, bars=bars)
            st = res.summary()
            sharpes.append(round(st.get("sharpe") or 0.0, 3))
        except Exception as e:
            log.warning("fold %d failed for %s: %s", i, strategy_id, e)
            sharpes.append(None)
    valid = [x for x in sharpes if x is not None]
    pos = sum(1 for x in valid if x > 0)
    return {"positive_ratio": (pos / len(valid)) if valid else None,
            "fold_sharpes": sharpes, "detail": "walk-forward test segments"}


# ------------------------------------------------------------------ sensitivity ----
def _sensitivity(strategy_id: str, params: dict, start: str, end: str,
                 bars: BarSource | None = None) -> dict:
    """Perturb key params around defaults (bounded, principled -- not a sweep)."""
    variants = []
    p = dict(params)
    if "top_n" in p:
        n = int(p["top_n"])
        for v in (max(5, n // 2), n * 2):
            q = dict(p, top_n=v)
            variants.append((f"top_n={v}", q))
    if "freq" in p and p["freq"] == "M":
        variants.append(("freq=W", dict(p, freq="W")))
    if "ma_long" in p:
        for v in (150, 250):
            variants.append((f"ma_long={v}", dict(p, ma_long=v)))
    if "mom_window" in p:
        for v in (40, 90):
            variants.append((f"mom_window={v}", dict(p, mom_window=v)))
    variants = variants[:4]
    sharpes = []
    for label, q in variants:
        try:
            _, res = run_backtest(strategy_id, q, start, end, scope="sensitivity",
                                  label=f"sens:{label}", persist=False, bars=bars)
            st = res.summary()
            sharpes.append({"variant": label, "sharpe": round(st.get("sharpe") or 0.0, 3),
                            "cagr": round(st.get("cagr") or 0.0, 4),
                            "mdd": round(st.get("mdd") or 0.0, 3)})
        except Exception as e:
            log.warning("sensitivity %s %s failed: %s", strategy_id, label, e)
    vals = [x["sharpe"] for x in sharpes]
    return {"variants": sharpes,
            "min_neighbor_sharpe": min(vals) if vals else None,
            "mean_neighbor_sharpe": float(np.mean(vals)) if vals else None,
            "all_positive": bool(vals) and all(v > 0 for v in vals)}


# ------------------------------------------------------------------ main eval ----
def evaluate_strategy(strategy_id: str, persist_eval: bool = True,
                      bars: BarSource | None = None) -> dict:
    if strategy_id not in STRATS:
        raise ValueError(f"unknown strategy {strategy_id}")
    rdy = readiness(strategy_id)
    _legacy_research_disabled()
    s = get_settings()
    clip_end = oos.research_clip_end()
    data_start = cal.open_days()[0] if cal.open_days() else s.history_start
    start = max(s.history_start, data_start)
    eval_id = uuid.uuid4().hex[:16]
    out = {"eval_id": eval_id, "strategy_id": strategy_id, "stage": "REJECT",
           "uses_oos": 0, "readiness": rdy}
    if not rdy["ready"]:
        out.update({"verdict": "REJECT", "notes": "data readiness not satisfied",
                    "gates": {"readiness": {"value": rdy, "pass": False}}})
        if persist_eval:
            _store_eval(out, clip_end)
        return out
    if start >= clip_end:
        out.update({"verdict": "RESEARCH", "notes": "research window too short (data still loading)"})
        if persist_eval:
            _store_eval(out, clip_end)
        return out

    strat_cls = STRATS[strategy_id]
    params = dict(strat_cls.default_params)
    target_pool = getattr(strat_cls, "target_pool", None)
    if bars is None:
        bars = BarSource(end=clip_end, tier="production", max_names=s.max_universe_names,
                         start=start, codes=target_pool)
    bt_id, res = run_backtest(strategy_id, params, start, clip_end,
                              scope="research", label="main", persist=True, bars=bars)
    stats = res.summary()
    folds = _walk_forward(strategy_id, params, start, clip_end, s.walkforward_folds, bars=bars)
    sens = _sensitivity(strategy_id, params, start, clip_end, bars=bars)
    boot = M.bootstrap_sharpe_ci(res.nav["ret"], s.bootstrap_blocks, s.bootstrap_block_len)
    verdict, gates = _rubric(stats, folds, sens, boot)
    stats.update({"walk_forward": folds, "sensitivity": sens, "bootstrap": boot,
                  "rolling_sharpe_min": _rolling_min(res.nav["ret"])})
    out.update({"verdict": verdict, "gates": gates, "stats": stats, "bt_id": bt_id})
    if persist_eval:
        _store_eval(out, clip_end)
    log.info("evaluate %s -> %s (sharpe=%.2f mdd=%.1f%%)", strategy_id, verdict,
             stats.get("sharpe", 0), 100 * stats.get("mdd", 0))
    return out


def _rolling_min(ret: pd.Series, window: int = 63) -> float | None:
    rs = M.rolling_sharpe(ret, window).dropna()
    return round(float(rs.min()), 3) if not rs.empty else None


def _store_eval(out: dict, scope_end: str) -> None:
    lab = db.connect_lab()
    try:
        row = {"eval_id": out["eval_id"], "strategy_id": out["strategy_id"],
               "stage": out["verdict"], "scope_start": get_settings().history_start,
               "scope_end": scope_end, "verdict": out["verdict"],
               "metrics_json": db.json_dumps(out.get("stats", {})),
               "gates_json": db.json_dumps(out.get("gates", {})),
               "uses_oos": out.get("uses_oos", 0), "engine_version": _engine_version(),
               "created_at": now_ts(), "notes": out.get("notes", "")}
        db.upsert(lab, "evaluations", [row],
                  ["eval_id", "strategy_id", "stage", "scope_start", "scope_end", "verdict",
                   "metrics_json", "gates_json", "uses_oos", "engine_version", "created_at", "notes"])
        _sync_strategy_status(lab, out["strategy_id"], out["verdict"])
    finally:
        lab.close()


def _sync_strategy_status(lab, strategy_id: str, verdict: str) -> None:
    cur = db.q1(lab, "SELECT status FROM strategy_registry WHERE strategy_id=?", (strategy_id,))
    # stage ladder never demotes CANDIDATE automatically
    order = {"REJECT": 0, "RESEARCH": 1, "PAPER": 2, "CANDIDATE": 3}
    old = order.get(cur["status"], 1) if cur else 1
    new = max(old, order.get(verdict, 1))
    status = [k for k, v in order.items() if v == new][0]
    if cur is None:
        db.upsert(lab, "strategy_registry",
                  [{"strategy_id": strategy_id, "name": strategy_id, "description": "",
                    "category": "", "params_json": "{}", "version": "", "created_at": now_ts(),
                    "status": status}],
                  ["strategy_id", "name", "description", "category", "params_json",
                   "version", "created_at", "status"])
    else:
        lab.execute("UPDATE strategy_registry SET status=? WHERE strategy_id=?",
                    (status, strategy_id))


# ------------------------------------------------------------------ OOS promotion ----
def consume_oos_and_promote(strategy_id: str) -> dict:
    """Spend the locked OOS window once for this strategy; final rating.
    The window becomes KNOWN permanently (visible in the OOS registry)."""
    _legacy_research_disabled()
    s = get_settings()
    oos.refresh_window("GLOBAL")
    start, end = oos.current_window()
    if not start:
        return {"error": "OOS window unavailable (not enough data)"}
    bt_id, res = run_backtest(strategy_id, dict(STRATS[strategy_id].default_params),
                              start, end, scope="oos", label="oos_final", consume_oos=True)
    stats = res.summary()
    verdict = "PAPER" if (stats.get("sharpe") or 0) > 0.2 and (stats.get("mdd") or 0) > -0.45 else "RESEARCH"
    lab = db.connect_lab()
    try:
        row = {"eval_id": uuid.uuid4().hex[:16], "strategy_id": strategy_id,
               "stage": verdict, "scope_start": start, "scope_end": end,
               "verdict": verdict, "metrics_json": db.json_dumps(stats),
               "gates_json": db.json_dumps({"oos_sharpe": {"value": stats.get("sharpe"),
                                                           "pass": (stats.get("sharpe") or 0) > 0.2}}),
               "uses_oos": 1, "engine_version": _engine_version(),
               "created_at": now_ts(), "notes": "OOS consumed (one-shot); window now KNOWN"}
        db.upsert(lab, "evaluations", [row],
                  ["eval_id", "strategy_id", "stage", "scope_start", "scope_end", "verdict",
                   "metrics_json", "gates_json", "uses_oos", "engine_version", "created_at", "notes"])
        if verdict == "PAPER":
            lab.execute("UPDATE strategy_registry SET status='PAPER' WHERE strategy_id=? "
                        "AND status IN ('RESEARCH','PAPER')", (strategy_id,))
    finally:
        lab.close()
    return {"bt_id": bt_id, "verdict": verdict, "stats": stats, "oos_window": [start, end]}
def _legacy_research_disabled() -> None:
    raise SafetyViolation(
        "legacy research evaluator is disabled by S03; use the V2 OOS AccessService and S06 pipeline"
    )
