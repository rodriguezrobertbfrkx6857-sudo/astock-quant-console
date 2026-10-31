"""Append-only SQLite persistence for S02 bitemporal facts."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.universe.models import (
    Board,
    Confidence,
    IdentifierVersion,
    RiskStatus,
    SecurityIdentity,
    SecurityStateVersion,
    SecurityType,
)
from src.quantcore.universe.repository import HistoricalSecurityMaster
from src.quantcore.universe.service import MembershipVersion

from .financials import FinancialRevision, FinancialRevisionStore


class PITCatalog:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)
        if not self.database.is_file():
            raise FileNotFoundError("migrated control.sqlite is required")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _insert(self, sql: str, params: tuple[object, ...]) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_identity(self, item: SecurityIdentity) -> None:
        self._insert(
            "INSERT INTO security_identities VALUES (?,?,?,?)",
            (item.asset_id, item.security_type.value, item.exchange, item.source_ref),
        )

    def append_identifier(self, identifier_id: str, item: IdentifierVersion) -> None:
        self._insert(
            "INSERT INTO security_identifier_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                identifier_id,
                item.asset_id,
                item.scheme,
                item.value,
                item.effective_from.isoformat(),
                item.effective_to.isoformat() if item.effective_to else None,
                item.known_from.isoformat(),
                item.known_to.isoformat() if item.known_to else None,
                item.source_ref,
                item.confidence.value,
            ),
        )

    def append_state(self, item: SecurityStateVersion) -> None:
        self._insert(
            "INSERT INTO security_state_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.state_id,
                item.asset_id,
                item.effective_from.isoformat(),
                item.effective_to.isoformat() if item.effective_to else None,
                item.known_from.isoformat(),
                item.known_to.isoformat() if item.known_to else None,
                item.list_date.isoformat() if item.list_date else None,
                item.delist_date.isoformat() if item.delist_date else None,
                item.name,
                item.board.value,
                item.risk_status.value,
                None if item.suspended is None else int(item.suspended),
                item.source_ref,
                item.confidence.value,
                item.revision,
            ),
        )

    def append_membership(self, membership_id: str, item: MembershipVersion) -> None:
        self._insert(
            "INSERT INTO universe_membership_versions VALUES (?,?,?,?,?,?,?,?)",
            (
                membership_id,
                item.group,
                item.asset_id,
                item.effective_from.isoformat(),
                item.effective_to.isoformat() if item.effective_to else None,
                item.available_at.isoformat(),
                item.source_ref,
                int(item.verified),
            ),
        )

    def append_financial(self, financial_id: str, item: FinancialRevision) -> None:
        self._insert(
            "INSERT INTO financial_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                financial_id,
                item.asset_id,
                item.statement,
                item.report_period.isoformat(),
                item.revision_id,
                item.available_at.isoformat(),
                item.observed_at.isoformat(),
                item.source_ref,
                item.source_row_hash,
                item.confidence.value,
                json.dumps(
                    item.values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    def load_master(self) -> HistoricalSecurityMaster:
        conn = self._connect()
        try:
            identities = [
                SecurityIdentity(
                    r["asset_id"],
                    SecurityType(r["security_type"]),
                    r["exchange"],
                    r["source_ref"],
                )
                for r in conn.execute(
                    "SELECT * FROM security_identities ORDER BY asset_id"
                )
            ]
            identifiers = [
                IdentifierVersion(
                    r["asset_id"],
                    r["scheme"],
                    r["value"],
                    date.fromisoformat(r["effective_from"]),
                    date.fromisoformat(r["effective_to"])
                    if r["effective_to"]
                    else None,
                    datetime.fromisoformat(r["known_from"]),
                    datetime.fromisoformat(r["known_to"]) if r["known_to"] else None,
                    r["source_ref"],
                    Confidence(r["confidence"]),
                )
                for r in conn.execute("SELECT * FROM security_identifier_versions")
            ]
            states = [
                SecurityStateVersion(
                    r["state_id"],
                    r["asset_id"],
                    date.fromisoformat(r["effective_from"]),
                    date.fromisoformat(r["effective_to"])
                    if r["effective_to"]
                    else None,
                    datetime.fromisoformat(r["known_from"]),
                    datetime.fromisoformat(r["known_to"]) if r["known_to"] else None,
                    date.fromisoformat(r["list_date"]) if r["list_date"] else None,
                    date.fromisoformat(r["delist_date"]) if r["delist_date"] else None,
                    r["name"],
                    Board(r["board"]),
                    RiskStatus(r["risk_status"]),
                    None if r["suspended"] is None else bool(r["suspended"]),
                    r["source_ref"],
                    Confidence(r["confidence"]),
                    r["revision"],
                )
                for r in conn.execute("SELECT * FROM security_state_versions")
            ]
        finally:
            conn.close()
        return HistoricalSecurityMaster(identities, identifiers, states)

    def load_financials(self) -> FinancialRevisionStore:
        conn = self._connect()
        try:
            rows = [
                FinancialRevision(
                    r["asset_id"],
                    r["statement"],
                    date.fromisoformat(r["report_period"]),
                    r["revision_id"],
                    datetime.fromisoformat(r["available_at"]),
                    datetime.fromisoformat(r["observed_at"]),
                    r["source_ref"],
                    r["source_row_hash"],
                    Confidence(r["confidence"]),
                    json.loads(r["values_json"]),
                )
                for r in conn.execute("SELECT * FROM financial_revisions")
            ]
        finally:
            conn.close()
        return FinancialRevisionStore(rows)
