from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary


class OutboxRelay:
    def __init__(self, accounting_database: str | Path, control_database: str | Path):
        self.accounting_database = PathBoundary().require_writable(accounting_database)
        self.control_database = PathBoundary().require_writable(control_database)

    def relay_once(self, *, fail_after_publish: bool = False) -> int:
        source = sqlite3.connect(self.accounting_database)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute(
                "SELECT * FROM accounting_outbox WHERE delivered_at IS NULL ORDER BY created_at,message_id"
            ).fetchall()
        finally:
            source.close()
        delivered = 0
        for row in rows:
            target = sqlite3.connect(self.control_database)
            try:
                target.execute("BEGIN IMMEDIATE")
                prior = target.execute(
                    "SELECT topic,payload_hash,payload_json FROM runtime_events WHERE message_id=?",
                    (row["message_id"],),
                ).fetchone()
                expected = (row["topic"], row["payload_hash"], row["payload_json"])
                if prior is not None and tuple(prior) != expected:
                    raise RuntimeError("runtime event conflict")
                target.execute(
                    "INSERT OR IGNORE INTO runtime_events VALUES (?,?,?,?,?)",
                    (row["message_id"],) + expected + (datetime.now(UTC).isoformat(),),
                )
                target.commit()
            except Exception:
                target.rollback()
                raise
            finally:
                target.close()
            if fail_after_publish:
                raise RuntimeError("fault injection after control publish")
            source = sqlite3.connect(self.accounting_database)
            try:
                source.execute(
                    "UPDATE accounting_outbox SET delivered_at=? WHERE message_id=? AND delivered_at IS NULL",
                    (datetime.now(UTC).isoformat(), row["message_id"]),
                )
                source.commit()
                delivered += 1
            finally:
                source.close()
        return delivered
