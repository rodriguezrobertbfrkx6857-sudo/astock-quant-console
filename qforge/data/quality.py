# -*- coding: utf-8 -*-
"""Data quality checks + strategy-specific data readiness."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from .. import db
from ..config import get_settings
from ..util import get_logger, now_ts, ymd

log = get_logger("qforge.quality")

# strategy-id -> required datasets with staleness policy (days)
STRATEGY_REQUIREMENTS: dict[str, list[dict]] = {
    "S1_DEFENSIVE_QUALITY": [{"dataset": "daily", "stale_days": 5},
                             {"dataset": "daily_basic", "stale_days": 5},
                             {"dataset": "adj_factor", "stale_days": 5},
                             {"dataset": "fina_indicator", "stale_days": 120, "optional": True}],
    "S2_MOMENTUM_12_1": [{"dataset": "daily", "stale_days": 5},
                         {"dataset": "adj_factor", "stale_days": 5}],
    "S3_REVERSAL_5D": [{"dataset": "daily", "stale_days": 5},
                       {"dataset": "adj_factor", "stale_days": 5}],
    "S4_ETF_ROTATION": [{"dataset": "etf_daily", "stale_days": 5}],
    "S5_INDEX_TREND_TIMING": [{"dataset": "bench_daily", "stale_days": 5}],
    "S6_EVENT_ALPHA": [{"dataset": "daily", "stale_days": 5},
                       {"dataset": "anns_d", "stale_days": 10, "optional": True},
                       {"dataset": "news", "stale_days": 10, "optional": True}],
    "S7_MULTI_FACTOR_VALUE": [{"dataset": "daily", "stale_days": 5},
                              {"dataset": "daily_basic", "stale_days": 5},
                              {"dataset": "adj_factor", "stale_days": 5}],
}


def check_bars() -> dict:
    mk = db.connect_market()
    try:
        mn_row = db.q1(mk, "SELECT MIN(trade_date) mn FROM bars")
        mx_row = db.q1(mk, "SELECT MAX(trade_date) mx FROM bars")
        row = db.q1(mk, """SELECT COUNT(*) n,
                                  SUM(CASE WHEN close IS NULL OR close<=0 THEN 1 ELSE 0 END) null_close,
                                  SUM(CASE WHEN high < low THEN 1 ELSE 0 END) bad_hl,
                                  SUM(CASE WHEN high < close OR low > close THEN 1 ELSE 0 END) bad_range,
                                  SUM(CASE WHEN vol IS NULL THEN 1 ELSE 0 END) null_vol
                           FROM bars""")
        n = row["n"] or 0
        return {"rows_total": n, "min_date": mn_row["mn"] if mn_row else None, "max_date": mx_row["mx"] if mx_row else None,
                "null_ratio": (row["null_close"] or 0) / max(1, n),
                "dup_ratio": 0.0,
                "bad_rows": int((row["bad_hl"] or 0) + (row["bad_range"] or 0)),
                "null_vol": row["null_vol"] or 0}
    finally:
        mk.close()


def _staleness(max_date: str | None) -> int:
    """Trading-day staleness: open sessions after the dataset's max date (<= today)."""
    if not max_date:
        return 9999
    from . import calendar as cal_mod
    try:
        od = cal_mod.open_days()
    except Exception:
        od = []
    if not od:
        d1 = dt.datetime.strptime(ymd(max_date), "%Y%m%d")
        return (dt.datetime.now() - d1).days
    today = dt.datetime.now().strftime("%Y%m%d")
    return len([d for d in od if ymd(max_date) < d <= today])


def _score(null_ratio: float, dup_ratio: float, stale_days: int, bad: int = 0) -> float:
    sc = 100.0
    sc -= min(40, null_ratio * 400)
    sc -= min(30, dup_ratio * 300)
    sc -= min(20, bad / 1000.0)
    if stale_days >= 10:
        sc -= 20
    elif stale_days >= 5:
        sc -= 10
    return max(0.0, sc)


def _status(score: float, stale_days: int) -> str:
    if score >= 85 and stale_days < 5:
        return "OK"
    if score >= 60:
        return "WARN"
    return "BAD"


def run_checks() -> list[dict]:
    s = get_settings()
    mk = db.connect_market()
    results: list[dict] = []
    try:
        specs = [
            ("bars", check_bars, None),
        ]
        # generic per-table span checks
        for table in ["norm_daily", "norm_daily_basic", "norm_stk_limit", "norm_suspend",
                      "bench_daily", "etf_daily", "norm_anns", "norm_news",
                      "norm_fina_indicator", "cal", "univ_daily"]:
            try:
                row = db.q1(mk, f"SELECT COUNT(*) n, MIN(trade_date) mn, MAX(trade_date) mx FROM {table}")
            except Exception:
                continue
            if row is None:
                continue
            stale = _staleness(row["mx"])
            sc = _score(0.0, 0.0, stale)
            results.append({"dataset": table, "checked_at": now_ts(), "rows_total": row["n"],
                            "min_date": row["mn"], "max_date": row["mx"], "stale_days": stale,
                            "null_ratio": 0.0, "dup_ratio": 0.0, "quality_score": sc,
                            "status": _status(sc, stale) if row["n"] else "EMPTY",
                            "detail": None})
        b = check_bars()
        stale = _staleness(b["max_date"])
        sc = _score(b["null_ratio"], b["dup_ratio"], stale, b["bad_rows"])
        results.append({"dataset": "bars", "checked_at": now_ts(), "rows_total": b["rows_total"],
                        "min_date": b["min_date"], "max_date": b["max_date"],
                        "stale_days": stale, "null_ratio": b["null_ratio"],
                        "dup_ratio": b["dup_ratio"], "quality_score": sc,
                        "status": _status(sc, stale) if b["rows_total"] else "EMPTY",
                        "detail": db.json_dumps({k: v for k, v in b.items()
                                                 if k not in ("min_date", "max_date")})})
        db.upsert(mk, "data_health", results,
                  ["dataset", "checked_at", "rows_total", "min_date", "max_date", "stale_days",
                   "null_ratio", "dup_ratio", "quality_score", "status", "detail"])
    finally:
        mk.close()
    return results


# logical dataset name -> data_health table name
DATASET_TABLE = {
    "daily": "norm_daily", "adj_factor": "norm_daily", "daily_basic": "norm_daily_basic",
    "stk_limit": "norm_stk_limit", "suspend_d": "norm_suspend", "bars": "bars",
    "bench_daily": "bench_daily", "etf_daily": "etf_daily", "fina_indicator": "norm_fina_indicator",
    "anns_d": "norm_anns", "news": "norm_news", "univ_daily": "univ_daily", "cal": "cal",
}


def readiness(strategy_id: str) -> dict:
    """Is the data this strategy needs present and fresh enough?"""
    mk = db.connect_market()
    try:
        reqs = STRATEGY_REQUIREMENTS.get(strategy_id, [{"dataset": "daily", "stale_days": 5}])
        checks = []
        ready = True
        for r in reqs:
            table = DATASET_TABLE.get(r["dataset"], r["dataset"])
            row = db.q1(mk, "SELECT max_date, stale_days, status FROM data_health WHERE dataset=?",
                        (table,))
            ok = bool(row) and row["stale_days"] is not None and row["stale_days"] <= r["stale_days"]
            if not ok:
                ready = ready and bool(r.get("optional"))
            checks.append({"dataset": r["dataset"], "ok": ok,
                           "optional": bool(r.get("optional")),
                           "stale_days": row["stale_days"] if row else None,
                           "status": row["status"] if row else "MISSING"})
        return {"strategy_id": strategy_id, "ready": ready, "checks": checks}
    finally:
        mk.close()


def all_readiness() -> list[dict]:
    from ..strategies import REGISTRY as STRATS
    return [readiness(sid) for sid in STRATS]
