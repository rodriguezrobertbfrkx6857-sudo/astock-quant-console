# -*- coding: utf-8 -*-
"""Historical universe access (PIT by construction -- date-keyed rows).

NEVER backfill today's listing state into history: every query is answered
from `univ_daily` rows built from list/delist dates, name history (ST),
suspension records and bar presence.

Tiers:
  research     -- listed, not suspended, has a bar (ST included, flagged)
  production   -- research + not ST + not newly listed + main boards only
"""
from __future__ import annotations

import functools

import pandas as pd

from .. import db
from ..util import ymd


@functools.lru_cache(maxsize=4)
def _load_day(trade_date: str) -> pd.DataFrame:
    mk = db.connect_market()
    try:
        return pd.read_sql_query(
            "SELECT * FROM univ_daily WHERE trade_date=?", mk, params=(ymd(trade_date),))
    finally:
        mk.close()


def on(trade_date: str) -> pd.DataFrame:
    """Full historical universe snapshot for one date (cache-hot)."""
    return _load_day(ymd(trade_date))


def eligible(trade_date: str, tier: str = "production") -> list[str]:
    df = on(trade_date)
    col = "eligible_production" if tier == "production" else "eligible_research"
    if df.empty:
        return []
    return df[df[col] == 1]["ts_code"].tolist()


def st_codes(trade_date: str) -> set[str]:
    df = on(trade_date)
    if df.empty:
        return set()
    return set(df[df["is_st"] == 1]["ts_code"])


def suspended_codes(trade_date: str) -> set[str]:
    df = on(trade_date)
    if df.empty:
        return set()
    return set(df[df["is_suspended"] == 1]["ts_code"])


def listed_codes(trade_date: str) -> list[str]:
    df = on(trade_date)
    if df.empty:
        return []
    return df[df["is_listed"] == 1]["ts_code"].tolist()


def info(ts_code: str) -> dict | None:
    mk = db.connect_market()
    try:
        r = db.q1(mk, "SELECT * FROM sec_master WHERE ts_code=?", (ts_code,))
        return dict(r) if r else None
    finally:
        mk.close()


def security_type(ts_code: str) -> str | None:
    i = info(ts_code)
    return i["sec_type"] if i else None


def coverage() -> dict:
    mk = db.connect_market()
    try:
        row = db.q1(mk, """SELECT COUNT(DISTINCT trade_date) days, COUNT(*) n_rows,
                                  MIN(trade_date) mn, MAX(trade_date) mx
                           FROM univ_daily""")
        prod = db.q1(mk, "SELECT COUNT(DISTINCT ts_code) c FROM univ_daily WHERE eligible_production=1")
        return {"days": row["days"], "rows": row["n_rows"], "min": row["mn"], "max": row["mx"],
                "production_eligible_securities": prod["c"] if prod else 0}
    finally:
        mk.close()
