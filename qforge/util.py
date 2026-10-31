# -*- coding: utf-8 -*-
"""Shared utilities: dates, logging, hashing, json."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_SH = ZoneInfo("Asia/Shanghai")

EOD_DECISION_TIME = "17:00:00"   # signals decided at T 17:00 CST; executed T+1


# ------------------------------------------------------------------ dates ----
def today_str() -> str:
    return dt.datetime.now(TZ_SH).strftime("%Y%m%d")


def now_ts() -> str:
    return dt.datetime.now(TZ_SH).isoformat(timespec="seconds")


def ymd(d: str | dt.date) -> str:
    if isinstance(d, dt.date):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "")[:8]


def to_date(d: str | dt.date) -> dt.date:
    if isinstance(d, dt.date):
        return d
    return dt.datetime.strptime(ymd(d), "%Y%m%d").date()


def iso(d: str | dt.date) -> str:
    return to_date(d).isoformat()


def available_at_for_bar(trade_date: str, hour: int = 17) -> str:
    """Bars for T become knowable at T 17:00 CST (vendor updates 15-17h)."""
    d = to_date(trade_date)
    return f"{d.isoformat()}T{hour:02d}:00:00+08:00"


def available_at_next_day(d: str | dt.date) -> str:
    """Conservative PIT stamp for disclosures: usable from next calendar day."""
    return (to_date(d) + dt.timedelta(days=1)).isoformat() + "T00:00:00+08:00"


def decision_ts(trade_date: str | dt.date) -> str:
    """The PIT decision instant for an EOD signal generated on trade_date."""
    return f"{to_date(trade_date).isoformat()}T{EOD_DECISION_TIME}+08:00"


def next_trade_day(cal: list[str], d: str | dt.date) -> str | None:
    ds = ymd(d)
    for c in cal:
        if c > ds:
            return c
    return None


def prev_trade_day(cal: list[str], d: str | dt.date) -> str | None:
    ds = ymd(d)
    prev = None
    for c in cal:
        if c >= ds:
            break
        prev = c
    return prev


def daterange_days(start: str, end: str):
    a, b = to_date(start), to_date(end)
    d = a
    while d <= b:
        yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------- hashing ----
def sha1(obj) -> str:
    if isinstance(obj, (dict, list, tuple)):
        payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    else:
        payload = str(obj)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(s):
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------- logging ----
_CONFIGURED = False


def get_logger(name: str = "qforge") -> logging.Logger:
    global _CONFIGURED
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    if not _CONFIGURED:
        log_dir.mkdir(exist_ok=True)
        root = logging.getLogger("qforge")
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = logging.FileHandler(log_dir / "qforge.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)
        _CONFIGURED = True
    return logging.getLogger(name)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def _scrub(text: str) -> str:
    from . import secrets as _sec
    return _sec.redact(text)
