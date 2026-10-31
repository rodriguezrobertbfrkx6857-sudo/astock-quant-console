# -*- coding: utf-8 -*-
"""Run Ledger: idempotent, resumable job execution.

Every (job, business_date) pair runs at most once successfully. Crashed runs
(stale heartbeat) are recovered on boot; failures retry with bounded attempts.
"""
from __future__ import annotations

import uuid

from .. import db
from ..util import get_logger, now_ts

log = get_logger("qforge.ledger")

MAX_ATTEMPTS = 3
STALE_SEC = 1800


class RunLedger:
    def __init__(self):
        self.lab = db.connect_lab()

    def close(self):
        self.lab.close()

    # ------------------------------------------------------------------ core
    def recover_stale(self, stale_sec: int = STALE_SEC) -> int:
        """Crash recovery: RUNNING runs with stale heartbeat -> FAILED (retryable)."""
        import datetime as dt
        cut = (dt.datetime.now() - dt.timedelta(seconds=stale_sec)).isoformat(timespec="seconds")
        rows = db.q(self.lab, "SELECT run_id, job, business_date FROM run_ledger "
                              "WHERE status='RUNNING' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                    (cut,))
        for r in rows:
            self.lab.execute(
                "UPDATE run_ledger SET status='FAILED', error=?, finished_at=? WHERE run_id=?",
                ("recovered: stale heartbeat (crash?)", now_ts(), r["run_id"]))
        if rows:
            log.warning("recovered %d stale runs", len(rows))
        return len(rows)

    def status(self, job: str, business_date: str) -> str | None:
        r = db.q1(self.lab, "SELECT status FROM run_ledger WHERE job=? AND business_date=?",
                  (job, business_date))
        return r["status"] if r else None

    def claim(self, job: str, business_date: str) -> tuple[str | None, str]:
        """Returns (run_id, action): action in {'skip','run','retry_maxed'}."""
        row = db.q1(self.lab, "SELECT * FROM run_ledger WHERE job=? AND business_date=?",
                    (job, business_date))
        if row and row["status"] == "SUCCEEDED":
            return row["run_id"], "skip"
        if row and row["status"] == "RUNNING":
            # another live process may own it; recover_stale decides
            return row["run_id"], "skip"
        if row and row["attempts"] >= MAX_ATTEMPTS and row["status"] == "FAILED":
            return row["run_id"], "retry_maxed"
        if row:
            run_id = row["run_id"]
            self.lab.execute("UPDATE run_ledger SET status='PENDING' WHERE run_id=?", (run_id,))
        else:
            run_id = uuid.uuid4().hex[:16]
            self.lab.execute(
                "INSERT INTO run_ledger (run_id, job, business_date, status, attempts) "
                "VALUES (?,?,?,?,0)", (run_id, job, business_date, "PENDING"))
        return run_id, "run"

    def start(self, run_id: str, checkpoint: dict | None = None) -> None:
        row = db.q1(self.lab, "SELECT attempts FROM run_ledger WHERE run_id=?", (run_id,))
        self.lab.execute(
            "UPDATE run_ledger SET status='RUNNING', started_at=?, heartbeat_at=?, attempts=?, "
            "checkpoint_json=? WHERE run_id=?",
            (now_ts(), now_ts(), (row["attempts"] if row else 0) + 1,
             db.json_dumps(checkpoint or {}), run_id))

    def heartbeat(self, run_id: str, checkpoint: dict | None = None) -> None:
        self.lab.execute(
            "UPDATE run_ledger SET heartbeat_at=?, checkpoint_json=? WHERE run_id=?",
            (now_ts(), db.json_dumps(checkpoint or {}), run_id))

    def finish(self, run_id: str, ok: bool, error: str | None = None,
               checkpoint: dict | None = None) -> None:
        self.lab.execute(
            "UPDATE run_ledger SET status=?, finished_at=?, error=?, checkpoint_json=? WHERE run_id=?",
            ("SUCCEEDED" if ok else "FAILED", now_ts(), error,
             db.json_dumps(checkpoint or {}), run_id))

    def last_success(self, job: str) -> str | None:
        r = db.q1(self.lab, "SELECT MAX(business_date) d FROM run_ledger WHERE job=? AND status='SUCCEEDED'",
                  (job,))
        return r["d"] if r else None

    def recent(self, n: int = 50) -> list[dict]:
        rows = db.q(self.lab, "SELECT * FROM run_ledger ORDER BY started_at DESC LIMIT ?", (n,))
        return [dict(r) for r in rows]

    def alert(self, level: str, source: str, message: str) -> None:
        self.lab.execute("INSERT INTO alerts (ts, level, source, message) VALUES (?,?,?,?)",
                         (now_ts(), level, source, message))
        log.warning("alert [%s] %s: %s", level, source, message)
        try:
            from .notify import send_notification
            send_notification(f"[{level}] {source}", message, level=level)
        except Exception:
            pass
