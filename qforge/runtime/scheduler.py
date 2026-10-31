# -*- coding: utf-8 -*-
"""Quant Runtime scheduler -- the long-running platform process.

Loop (every ~20s):
- recover stale runs (crash recovery)
- decide today's EOD business date:
    * after eod_run_time on an open day  -> that day
    * otherwise                          -> the previous open day (catch-up)
- if the `eod` chain has not SUCCEEDED for that date -> run the full chain
  (sync -> build -> quality -> features -> events -> research -> paper ->
   validate); every sub-job is ledger-guarded, idempotent and isolated.
- write heartbeat + state for the dashboard; exit cleanly on SIGINT/TERM.

The EOD chain needs no realtime feed and no LLM (offline-capable by design).
"""
from __future__ import annotations

import datetime as dt
import signal
import sys
import time
from pathlib import Path

from .. import db
from ..config import get_settings
from ..data import calendar as cal
from ..util import get_logger, now_ts
from .jobs import run_eod_chain
from .ledger import RunLedger

log = get_logger("qforge.runtime")

PID_FILE = Path(__file__).resolve().parents[2] / "logs" / "runtime.pid"


class Runtime:
    def __init__(self):
        self.s = get_settings()
        self.stop = False

    # ------------------------------------------------------------------ signals
    def _handle(self, signum, frame):
        log.info("runtime received signal %s; stopping", signum)
        self.stop = True

    # ------------------------------------------------------------------ helpers
    def _target_bdate(self, now: dt.datetime) -> str | None:
        """The business date whose EOD should be complete right now."""
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y%m%d")
        if cal.is_open(today) and hhmm >= self.s.eod_run_time:
            return today
        # previous open day (catch-up)
        od = [d for d in cal.open_days() if d < today]
        return od[-1] if od else None

    def _write_state(self, status: str, detail: dict | None = None) -> None:
        mk = db.connect_market()
        try:
            db.kv_set(mk, "runtime_state", db.json_dumps({
                "status": status, "ts": now_ts(), "pid": PID_FILE.stem and _pid(),
                "detail": detail or {}}))
        finally:
            mk.close()

    # ------------------------------------------------------------------ main
    def run(self, once: bool = False) -> int:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)
        PID_FILE.parent.mkdir(exist_ok=True)
        PID_FILE.write_text(str(_pid()), encoding="utf-8")
        log.info("runtime started (pid=%s), EOD at %s, research at %s",
                 _pid(), self.s.eod_run_time, self.s.research_run_time)
        last_run_date = None
        ledger = RunLedger()
        try:
            while not self.stop:
                try:
                    ledger.recover_stale()
                    now = dt.datetime.now()
                    bdate = self._target_bdate(now)
                    if bdate is None:
                        self._write_state("WAITING_CALENDAR")
                    else:
                        done = ledger.status("eod", bdate) == "SUCCEEDED"
                        if not done and bdate != last_run_date:
                            log.info("running EOD chain for %s", bdate)
                            self._write_state("RUNNING_EOD", {"bdate": bdate})
                            out = run_eod_chain(bdate)
                            failed = [k for k, v in out["jobs"].items() if v["status"] == "FAILED"]
                            if failed:
                                ledger.alert("WARN", "eod", f"chain had failures: {failed}")
                            last_run_date = bdate
                            log.info("EOD chain for %s finished: %s", bdate,
                                     {k: v["status"] for k, v in out["jobs"].items()})
                        self._write_state("IDLE", {"bdate": bdate, "eod_done": done})
                except Exception:
                    log.exception("runtime loop error")
                    self._write_state("ERROR", {"error": "loop exception, will retry"})
                if once:
                    break
                for _ in range(4):  # 20s in 5s slices, responsive to signals
                    if self.stop:
                        break
                    time.sleep(5)
        finally:
            ledger.close()
            self._write_state("STOPPED")
            try:
                PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            log.info("runtime stopped")
        return 0


def _pid() -> int:
    import os
    return os.getpid()


def main() -> int:
    return Runtime().run()
