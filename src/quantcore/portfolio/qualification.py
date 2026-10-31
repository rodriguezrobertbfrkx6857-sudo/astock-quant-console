from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary


@dataclass(frozen=True)
class Qualification:
    qualification_id: str
    strategy_version_id: str
    stage09_result_id: str
    stage10_result_id: str
    protocol_hash: str
    snapshot_hash: str
    status: str


class QualificationService:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def _connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def issue(
        self, strategy_version_id: str, stage09_result_id: str, stage10_result_id: str
    ) -> Qualification:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT sv.protocol_hash,sv.snapshot_hash,s9.rating,s9.protocol_hash AS s9_protocol,s9.snapshot_hash AS s9_snapshot,s10.status,s10.input_hash,s10.evidence_valid,s10.research_run_id,e.research_run_id AS evidence_run_id,e.snapshot_hash AS evidence_snapshot,rq.status AS queue_status "
                "FROM strategy_versions sv JOIN stage09_results s9 ON s9.result_id=? JOIN research_queue rq ON rq.queue_item_id=s9.queue_item_id AND rq.strategy_version_id=sv.strategy_version_id JOIN stage10_runs s10 ON s10.result_id=? LEFT JOIN stage09_evidence e ON e.result_id=s9.result_id WHERE sv.strategy_version_id=?",
                (stage09_result_id, stage10_result_id, strategy_version_id),
            ).fetchone()
            if row is None:
                raise ValueError("qualification evidence not found")
            if (
                row["queue_status"] != "SUCCEEDED"
                or row["rating"] != "CANDIDATE"
                or row["status"] != "PASS"
                or row["evidence_valid"] != 1
            ):
                raise ValueError(
                    "qualification requires CANDIDATE and valid Stage10 PASS"
                )
            expected_research_run = row["evidence_run_id"] or stage09_result_id
            binding_snapshot = row["evidence_snapshot"] or row["s9_snapshot"]
            if (
                row["research_run_id"] != expected_research_run
                or row["protocol_hash"] != row["s9_protocol"]
                or (row["evidence_snapshot"] is None and row["snapshot_hash"] != row["s9_snapshot"])
                or row["input_hash"] != binding_snapshot
            ):
                raise ValueError("qualification evidence bindings do not match")
            qualification_id = hashlib.sha256(
                f"{strategy_version_id}|{stage09_result_id}|{stage10_result_id}".encode()
            ).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO paper_qualifications VALUES (?,?,?,?,?,?,?)",
                (
                    qualification_id,
                    strategy_version_id,
                    stage09_result_id,
                    stage10_result_id,
                    row["protocol_hash"],
                    binding_snapshot,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO qualification_events(event_id,qualification_id,status,reason,created_at) VALUES (?,?,?,?,?)",
                (
                    f"{qualification_id}:ACTIVE",
                    qualification_id,
                    "ACTIVE",
                    "evidence verified",
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
            return self.get(qualification_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def revoke(self, qualification_id: str, reason: str) -> None:
        if not reason:
            raise ValueError("revocation reason required")
        conn = self._connect()
        try:
            event_id = hashlib.sha256(
                f"{qualification_id}|REVOKED|{reason}".encode()
            ).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO qualification_events VALUES (?,?,?,?,?)",
                (
                    event_id,
                    qualification_id,
                    "REVOKED",
                    reason,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, qualification_id: str) -> Qualification:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT q.*,e.status FROM paper_qualifications q JOIN qualification_events e ON e.qualification_id=q.qualification_id WHERE q.qualification_id=? ORDER BY e.rowid DESC LIMIT 1",
                (qualification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(qualification_id)
            return Qualification(
                row["qualification_id"],
                row["strategy_version_id"],
                row["stage09_result_id"],
                row["stage10_result_id"],
                row["protocol_hash"],
                row["snapshot_hash"],
                row["status"],
            )
        finally:
            conn.close()

    def require_active(
        self, qualification_id: str, *, snapshot_hash: str
    ) -> Qualification:
        item = self.get(qualification_id)
        if item.status != "ACTIVE" or item.snapshot_hash != snapshot_hash:
            raise PermissionError("PAPER qualification is inactive or stale")
        return item
