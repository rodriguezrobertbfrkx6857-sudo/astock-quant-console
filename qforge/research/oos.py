# -*- coding: utf-8 -*-
"""OOS (out-of-sample) registry -- LOCKED windows can only be spent once.

Integrity model:
- The OOS window is the most recent `oos_trading_days` sessions relative to the
  data horizon. Research/parameter work must never touch it.
- `assert_not_oos()` raises OOSViolation if a research step's window overlaps a
  LOCKED window.
- `acquire()` explicitly spends a window: LOCKED -> KNOWN permanently (audit
  recorded). Once KNOWN, results read from it are still reported but flagged
  uses_oos=1 and can never again be presented as untouched OOS.
- Any future window overlapping a KNOWN one inherits KNOWN (no re-rolling).
"""
from __future__ import annotations

import uuid

from .. import db
from ..config import get_settings
from ..data import calendar as cal
from ..util import get_logger, now_ts

log = get_logger("qforge.oos")


class OOSViolation(RuntimeError):
    pass


def data_end() -> str:
    """Latest available curated bar date (data horizon)."""
    mk = db.connect_market()
    try:
        r = db.q1(mk, "SELECT MAX(trade_date) mx FROM bars")
        return r["mx"] if r and r["mx"] else (cal.last_open() or "19000101")
    finally:
        mk.close()


def current_window() -> tuple[str, str]:
    """(start, end) of the currently locked OOS window."""
    s = get_settings()
    end = data_end()
    od = [d for d in cal.open_days() if d <= end]
    if len(od) < s.oos_trading_days + 1:
        return ("", end)
    return (od[-1 - s.oos_trading_days], end)


def _rows(scope: str | None = None):
    lab = db.connect_lab()
    try:
        # KNOWN status is global: any consumed window is known for everyone
        return db.q(lab, "SELECT * FROM oos_registry ORDER BY start")
    finally:
        lab.close()


def _overlap(a1: str, a2: str, b1: str, b2: str) -> bool:
    return a1 <= b2 and b1 <= a2


def refresh_window(scope: str = "GLOBAL") -> dict:
    """Ensure a registry row exists for the current window; inherits KNOWN if it
    overlaps any previously KNOWN window (strictly no fresh-OOS recycling).

    Stale LOCKED reservations (windows that are no longer the current OOS and
    were never consumed) are cleared -- only KNOWN rows are permanent.
    """
    start, end = current_window()
    lab = db.connect_lab()
    try:
        # release expired reservations: LOCKED rows are always re-derivable
        lab.execute("DELETE FROM oos_registry WHERE status='LOCKED'")
        existing = db.q(lab, "SELECT * FROM oos_registry WHERE scope=? AND start=? AND end=?",
                        (scope, start, end))
        if existing:
            return dict(existing[0])
        known = db.q(lab, "SELECT * FROM oos_registry WHERE status='KNOWN'")
        tainted = any(_overlap(start, end, k["start"], k["end"]) for k in known)
        rid = uuid.uuid4().hex[:16]
        row = {"id": rid, "scope": scope, "start": start, "end": end,
               "status": "KNOWN" if tainted else "LOCKED",
               "consumed_at": now_ts() if tainted else None,
               "consumed_by": "inherited" if tainted else None,
               "note": "inherited KNOWN (overlaps a consumed window)" if tainted else "",
               "created_at": now_ts()}
        db.upsert(lab, "oos_registry", [row],
                  ["id", "scope", "start", "end", "status", "consumed_at", "consumed_by",
                   "note", "created_at"])
        return row
    finally:
        lab.close()


def status_for_window(start: str, end: str, scope: str = "GLOBAL") -> str:
    """LOCKED / KNOWN / FREE for a concrete window.

    FREE = no registry row and no overlap with the currently computable OOS
    window (i.e. a plain historical segment, safe for research).
    """
    rows = _rows()
    for r in rows:
        if _overlap(start, end, r["start"], r["end"]):
            return r["status"]
    cs, ce = current_window()
    if cs and _overlap(start, end, cs, ce):
        return "LOCKED"
    return "FREE"


def assert_not_oos(start: str, end: str, scope: str = "GLOBAL", what: str = "research") -> None:
    """Hard gate: research/parameter work must not touch a LOCKED OOS window."""
    st = status_for_window(start, end, scope)
    if st == "LOCKED":
        cur_s, cur_e = current_window()
        raise OOSViolation(
            f"{what}: window [{start},{end}] intersects the LOCKED OOS window [{cur_s},{cur_e}]; "
            f"clip your research to end < {cur_s} or explicitly consume OOS via oos.acquire()")
    # KNOWN windows are allowed but must be reported as spent (callers flag it)


def acquire(scope: str, start: str, end: str, consumed_by: str = "promotion") -> dict:
    """Spend the OOS window once (per scope). Returns the registry row."""
    lab = db.connect_lab()
    try:
        rows = db.q(lab, "SELECT * FROM oos_registry WHERE scope=?", (scope,))
        target = None
        for r in rows:
            if _overlap(start, end, r["start"], r["end"]):
                target = r
                break
        if target is None:
            rid = uuid.uuid4().hex[:16]
            target = {"id": rid, "scope": scope, "start": start, "end": end,
                      "status": "LOCKED", "consumed_at": None, "consumed_by": None,
                      "note": "", "created_at": now_ts()}
            db.upsert(lab, "oos_registry", [target],
                      ["id", "scope", "start", "end", "status", "consumed_at",
                       "consumed_by", "note", "created_at"])
        if target["status"] == "KNOWN":
            return dict(target)  # already spent; caller must flag uses_oos
        db.upsert(lab, "oos_registry",
                  [{"id": target["id"], "scope": scope, "start": target["start"],
                    "end": target["end"], "status": "KNOWN", "consumed_at": now_ts(),
                    "consumed_by": consumed_by,
                    "note": "consumed (results are no longer untouched OOS)",
                    "created_at": target["created_at"]}],
                  ["id", "scope", "start", "end", "status", "consumed_at", "consumed_by",
                   "note", "created_at"])
        updated = db.q1(lab, "SELECT * FROM oos_registry WHERE id=?", (target["id"],))
        # mirror consumption on the GLOBAL row so the window is KNOWN for everyone
        g = db.q1(lab, "SELECT * FROM oos_registry WHERE scope='GLOBAL' AND start=? AND end=?",
                  (target["start"], target["end"]))
        if g and g["status"] != "KNOWN":
            db.upsert(lab, "oos_registry",
                      [{"id": g["id"], "scope": "GLOBAL", "start": g["start"], "end": g["end"],
                        "status": "KNOWN", "consumed_at": now_ts(), "consumed_by": consumed_by,
                        "note": f"consumed via {scope}", "created_at": g["created_at"]}],
                      ["id", "scope", "start", "end", "status", "consumed_at", "consumed_by",
                       "note", "created_at"])
        log.warning("OOS window [%s,%s] scope=%s CONSUMED by %s", target["start"],
                    target["end"], scope, consumed_by)
        return dict(updated)
    finally:
        lab.close()


def research_clip_end() -> str:
    """Latest date usable for TRAIN/VALIDATION research (before locked OOS)."""
    start, _ = current_window()
    if not start:
        return data_end()
    od = [d for d in cal.open_days() if d < start]
    return od[-1] if od else start


def summary() -> dict:
    rows = _rows()
    return {"current_window": current_window(), "research_end": research_clip_end(),
            "windows": [dict(r) for r in rows]}
