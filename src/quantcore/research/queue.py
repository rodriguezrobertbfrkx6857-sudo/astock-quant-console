from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.research.registry import (
    QueueStatus,
    ResearchProtocol,
    Stage09Result,
    StrategyVersion,
)


_TRANSITIONS = {
    QueueStatus.PLANNED: {
        QueueStatus.BLOCKED_DATA,
        QueueStatus.READY,
        QueueStatus.CANCELLED,
    },
    QueueStatus.BLOCKED_DATA: {QueueStatus.READY, QueueStatus.CANCELLED},
    QueueStatus.READY: {QueueStatus.RUNNING, QueueStatus.CANCELLED},
    QueueStatus.RUNNING: {
        QueueStatus.SUCCEEDED,
        QueueStatus.FAILED,
        QueueStatus.CANCELLED,
    },
    QueueStatus.SUCCEEDED: set(),
    QueueStatus.FAILED: {QueueStatus.READY},
    QueueStatus.CANCELLED: set(),
}


class ResearchStore:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def _connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register(
        self,
        protocol: ResearchProtocol,
        version: StrategyVersion,
        queue_item_id: str,
        priority: int,
    ) -> None:
        if version.protocol_hash != protocol.protocol_hash:
            raise ValueError("protocol/version mismatch")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO strategy_families(family_id,control_only) VALUES (?,?)",
                (protocol.family_id, int(protocol.control_only)),
            )
            import json
            from dataclasses import asdict

            protocol_json = json.dumps(
                asdict(protocol),
                default=str,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO research_protocols VALUES (?,?,?,?,?)",
                (
                    protocol.protocol_hash,
                    protocol.protocol_id,
                    protocol.family_id,
                    protocol_json,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO strategy_versions VALUES (?,?,?,?,?,?,?)",
                (
                    version.strategy_version_id,
                    version.family_id,
                    version.protocol_hash,
                    version.source_code_hash,
                    version.snapshot_hash,
                    version.universe_version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO research_queue(queue_item_id,strategy_version_id,protocol_hash,priority,status,attempt,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    queue_item_id,
                    version.strategy_version_id,
                    version.protocol_hash,
                    priority,
                    QueueStatus.PLANNED.value,
                    0,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transition(self, queue_item_id: str, target: QueueStatus) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM research_queue WHERE queue_item_id=?",
                (queue_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(queue_item_id)
            current = QueueStatus(row["status"])
            if target not in _TRANSITIONS[current]:
                raise ValueError(f"invalid queue transition {current}->{target}")
            attempt = 1 if target is QueueStatus.RUNNING else 0
            conn.execute(
                "UPDATE research_queue SET status=?,attempt=attempt+?,updated_at=? WHERE queue_item_id=?",
                (target.value, attempt, datetime.now(UTC).isoformat(), queue_item_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_stage09(
        self, result_id: str, queue_item_id: str, result: Stage09Result
    ) -> None:
        import json

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            status = conn.execute(
                "SELECT status FROM research_queue WHERE queue_item_id=?",
                (queue_item_id,),
            ).fetchone()
            if status is None or status[0] != QueueStatus.SUCCEEDED.value:
                raise ValueError("Stage09 requires a successfully completed task")
            conn.execute(
                "INSERT INTO stage09_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    result_id,
                    queue_item_id,
                    result.protocol_hash,
                    result.snapshot_hash,
                    result.rating.value,
                    json.dumps(result.reasons),
                    str(result.primary_metric)
                    if result.primary_metric is not None
                    else None,
                    result.effective_trades,
                    result.validation_blocks,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
