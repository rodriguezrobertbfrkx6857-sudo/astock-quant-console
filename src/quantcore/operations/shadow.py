from __future__ import annotations
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from src.quantcore.contracts.safety import PathBoundary


class ShadowTracker:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def start(self, run_id: str, target_sessions: int = 20) -> None:
        if target_sessions < 20:
            raise ValueError("long-run acceptance requires at least 20 actual sessions")
        conn = sqlite3.connect(self.database)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO shadow_acceptance_runs VALUES (?,?, 'ACTIVE',?,NULL)",
                (run_id, target_sessions, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def record_day(
        self,
        run_id: str,
        business_date: date,
        *,
        verified_trading_session: bool,
        trading_session_evidence: str,
        reconciliation_status: str,
        duplicate_fills: int,
        permission_violations: int,
        today: date | None = None,
    ) -> None:
        if not verified_trading_session or not trading_session_evidence:
            raise ValueError("verified exchange-calendar evidence is required")
        if business_date > (today or date.today()):
            raise ValueError("future shadow days cannot be recorded")
        if reconciliation_status not in {"PASS", "FAIL"}:
            raise ValueError("invalid reconciliation status")
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT target_sessions,status FROM shadow_acceptance_runs WHERE shadow_run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or row[1] != "ACTIVE":
                raise ValueError("shadow run is not active")
            conn.execute(
                "INSERT INTO shadow_acceptance_days VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    business_date.isoformat(),
                    trading_session_evidence,
                    reconciliation_status,
                    duplicate_fills,
                    permission_violations,
                    datetime.now(UTC).isoformat(),
                ),
            )
            count = conn.execute(
                "SELECT count(*) FROM shadow_acceptance_days WHERE shadow_run_id=? AND reconciliation_status='PASS' AND duplicate_fills=0 AND permission_violations=0",
                (run_id,),
            ).fetchone()[0]
            if count >= row[0]:
                conn.execute(
                    "UPDATE shadow_acceptance_runs SET status='COMPLETE',completed_at=? WHERE shadow_run_id=?",
                    (datetime.now(UTC).isoformat(), run_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self, run_id: str) -> dict[str, object]:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        try:
            run = conn.execute(
                "SELECT * FROM shadow_acceptance_runs WHERE shadow_run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            count = conn.execute(
                "SELECT count(*) FROM shadow_acceptance_days WHERE shadow_run_id=?",
                (run_id,),
            ).fetchone()[0]
            return {
                **dict(run),
                "recorded_sessions": count,
                "remaining_sessions": max(0, run["target_sessions"] - count),
            }
        finally:
            conn.close()
