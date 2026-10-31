"""Global, append-only OOS exposure authorization (S03)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from src.quantcore.contracts.safety import PathBoundary


class AccessDecision(StrEnum):
    ALLOW_KNOWN = "ALLOW_KNOWN"
    DENY_UNKNOWN = "DENY_UNKNOWN"
    DENY_RESERVED = "DENY_RESERVED"


class AccessPurpose(StrEnum):
    RESEARCH = "RESEARCH"
    CLI = "CLI"
    API = "API"
    CHART = "CHART"
    EVENT = "EVENT"


@dataclass(frozen=True)
class AccessReceipt:
    receipt_id: str
    decision: AccessDecision
    requested_start: date
    requested_end: date
    purpose: AccessPurpose
    observed_at: datetime
    reason: str


class OOSAccessService:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)
        if not self.database.is_file():
            raise FileNotFoundError("migrated control.sqlite required")

    def _connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _covered(start: date, end: date, rows: list[sqlite3.Row]) -> bool:
        intervals = sorted(
            (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
            for r in rows
        )
        cursor = start
        for left, right in intervals:
            if right < cursor:
                continue
            if left > cursor:
                return False
            cursor = max(cursor, right + timedelta(days=1))
            if cursor > end:
                return True
        return cursor > end

    def authorize_known(
        self,
        *,
        protocol_hash: str,
        snapshot_hash: str,
        start: date,
        end: date,
        purpose: AccessPurpose,
        asset_scope: str = "GLOBAL",
        family_scope: str = "GLOBAL",
    ) -> AccessReceipt:
        if start > end:
            raise ValueError("invalid requested range")
        if not re.fullmatch(r"[0-9a-f]{64}", protocol_hash) or not re.fullmatch(
            r"[0-9a-f]{64}", snapshot_hash
        ):
            raise ValueError("protocol and snapshot hashes must be lowercase sha256")
        conn = self._connect()
        now = datetime.now(UTC)
        receipt_id = f"access-{uuid4().hex}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            reserved = conn.execute(
                "SELECT 1 FROM window_reservations WHERE status='RESERVED' AND NOT(end_date<? OR start_date>?) AND (asset_scope='GLOBAL' OR asset_scope=?) AND (family_scope='GLOBAL' OR family_scope=?) LIMIT 1",
                (start.isoformat(), end.isoformat(), asset_scope, family_scope),
            ).fetchone()
            rows = conn.execute(
                "SELECT start_date,end_date FROM exposure_events WHERE provenance<>'SYNTHETIC' AND (asset_scope='GLOBAL' OR asset_scope=?) AND (family_scope='GLOBAL' OR family_scope=?)",
                (asset_scope, family_scope),
            ).fetchall()
            if reserved:
                decision, reason = (
                    AccessDecision.DENY_RESERVED,
                    "range intersects reserved holdout",
                )
            elif self._covered(start, end, rows):
                decision, reason = (
                    AccessDecision.ALLOW_KNOWN,
                    "requested range is fully covered by KNOWN exposure",
                )
            else:
                decision, reason = (
                    AccessDecision.DENY_UNKNOWN,
                    "UNKNOWN is not UNTOUCHED; current construction permits KNOWN only",
                )
            conn.execute(
                "INSERT INTO access_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    None,
                    protocol_hash,
                    snapshot_hash,
                    start.isoformat(),
                    end.isoformat(),
                    asset_scope,
                    family_scope,
                    purpose.value,
                    decision.value,
                    now.isoformat(),
                    reason,
                    None,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return AccessReceipt(receipt_id, decision, start, end, purpose, now, reason)


class ResultAccessGateway:
    """All user-visible and research entry points share one authorization call."""

    def __init__(self, service: OOSAccessService):
        self.service = service

    def access(self, entrypoint: AccessPurpose, **request) -> AccessReceipt:
        if not isinstance(entrypoint, AccessPurpose):
            raise ValueError("registered AccessPurpose required")
        return self.service.authorize_known(purpose=entrypoint, **request)


def import_exposure_file(database: str | Path, source: str | Path) -> dict[str, int]:
    database = PathBoundary().require_writable(database)
    source = PathBoundary().require_readable(source)
    payload = json.loads(source.read_text("utf-8"))
    inserted = duplicates = 0
    conn = sqlite3.connect(database)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in payload["events"]:
            raw = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            key = hashlib.sha256(raw.encode()).hexdigest()
            cursor = conn.execute(
                "INSERT OR IGNORE INTO exposure_events(event_id,import_key,observed_at,imported_at,source_project,source_path,source_sha256,run_id,artifact_hash,start_date,end_date,calendar_id,asset_scope,family_scope,provenance,evidence_kind,confidence,actor_scope,notes,supersedes_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["event_id"],
                    key,
                    item["observed_at"],
                    datetime.now(UTC).isoformat(),
                    item["source_project"],
                    item["source_path"],
                    item["source_sha256"],
                    item.get("run_id"),
                    item.get("artifact_hash"),
                    item["start"],
                    item["end"],
                    item.get("calendar_id", "CN_TRADING"),
                    item.get("asset_scope", "GLOBAL"),
                    item.get("family_scope", "GLOBAL"),
                    item["provenance"],
                    item["evidence_kind"],
                    item["confidence"],
                    item.get("actor_scope", "TEAM"),
                    item.get("notes"),
                    item.get("supersedes_event_id"),
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "total": len(payload["events"]),
    }
