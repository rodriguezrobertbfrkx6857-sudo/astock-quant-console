# -*- coding: utf-8 -*-
"""A-share trading calendar helpers (SSE calendar stored in curated `cal`)."""
from __future__ import annotations

import functools

from .. import db
from ..util import today_str, ymd


@functools.lru_cache(maxsize=1)
def _cal_tuple() -> tuple[list[str], set[str]]:
    mk = db.connect_market()
    try:
        rows = db.q(mk, "SELECT trade_date, is_open FROM cal ORDER BY trade_date")
        all_days = [r["trade_date"] for r in rows]
        open_days = {r["trade_date"] for r in rows if r["is_open"] == 1}
        return all_days, open_days
    finally:
        mk.close()


def invalidate_cache() -> None:
    _cal_tuple.cache_clear()


def all_days() -> list[str]:
    return _cal_tuple()[0]


def open_days() -> list[str]:
    return sorted(_cal_tuple()[1])


def is_open(d: str) -> bool:
    return ymd(d) in _cal_tuple()[1]


def next_open(d: str) -> str | None:
    ds = ymd(d)
    for x in open_days():
        if x > ds:
            return x
    return None


def prev_open(d: str) -> str | None:
    ds = ymd(d)
    prev = None
    for x in open_days():
        if x >= ds:
            break
        prev = x
    return prev


def last_open(asof: str | None = None) -> str | None:
    """Latest open session on or before `asof` (default: today, real clock)."""
    ds = ymd(asof) if asof else today_str()
    last = None
    for x in open_days():
        if x > ds:
            break
        last = x
    return last


def open_days_between(start: str, end: str) -> list[str]:
    s, e = ymd(start), ymd(end)
    return [x for x in open_days() if s <= x <= e]


def sessions_between(start: str, end: str) -> list[str]:
    return open_days_between(start, end)


def shift(d: str, n: int) -> str | None:
    """n-th open day after (n>0) / before (n<0) date d (n=0 -> d if open)."""
    od = open_days()
    ds = ymd(d)
    if ds in od:
        i = od.index(ds)
        j = i + n
        return od[j] if 0 <= j < len(od) else None
    for x in od:
        if x > ds:
            i = od.index(x)
            j = i + (n - 1 if n > 0 else n)
            return od[j] if 0 <= j < len(od) else None
    return None
