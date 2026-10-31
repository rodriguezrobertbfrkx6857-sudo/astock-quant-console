from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")


def _hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("content hash must be sha256")


@dataclass(frozen=True)
class EventDocument:
    event_id: str
    source_url: str
    source_name: str
    published_at: datetime
    first_seen_at: datetime
    raw_object_hash: str
    provenance: str

    def __post_init__(self):
        _aware(self.published_at)
        _aware(self.first_seen_at)
        _hash(self.raw_object_hash)
        if self.first_seen_at < self.published_at:
            raise ValueError("first_seen_at cannot precede publication")
        if not self.source_url or self.provenance not in {
            "REAL",
            "SYNTHETIC",
            "UNKNOWN",
            "MIXED",
        }:
            raise ValueError("invalid event provenance")


@dataclass(frozen=True)
class EventRevision:
    event_id: str
    revision_id: str
    available_at: datetime
    text_hash: str
    title: str
    category: str | None = None

    def __post_init__(self):
        _aware(self.available_at)
        _hash(self.text_hash)


class EventStore:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def add(self, document: EventDocument, revision: EventRevision) -> None:
        if (
            document.event_id != revision.event_id
            or revision.available_at < document.first_seen_at
        ):
            raise ValueError("event revision timing mismatch")
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("BEGIN IMMEDIATE")
            document_values = (
                document.event_id,
                document.source_url,
                document.source_name,
                document.published_at.isoformat(),
                document.first_seen_at.isoformat(),
                document.raw_object_hash,
                document.provenance,
            )
            existing_document = conn.execute(
                "SELECT event_id,source_url,source_name,published_at,first_seen_at,raw_object_hash,provenance "
                "FROM event_documents WHERE event_id=?",
                (document.event_id,),
            ).fetchone()
            if existing_document is None:
                conn.execute(
                    "INSERT INTO event_documents VALUES (?,?,?,?,?,?,?,?)",
                    document_values + (datetime.now(UTC).isoformat(),),
                )
            elif tuple(existing_document) != document_values:
                raise RuntimeError("immutable event document conflict")
            revision_values = (
                revision.event_id,
                revision.revision_id,
                revision.available_at.isoformat(),
                revision.text_hash,
                revision.title,
                revision.category,
            )
            existing_revision = conn.execute(
                "SELECT event_id,revision_id,available_at,text_hash,title,category "
                "FROM event_revisions WHERE event_id=? AND revision_id=?",
                (revision.event_id, revision.revision_id),
            ).fetchone()
            if existing_revision is None:
                conflicting_revision = conn.execute(
                    "SELECT event_id,revision_id,available_at,text_hash,title,category "
                    "FROM event_revisions WHERE event_id=? AND available_at=? AND text_hash=?",
                    (
                        revision.event_id,
                        revision.available_at.isoformat(),
                        revision.text_hash,
                    ),
                ).fetchone()
                if conflicting_revision is not None:
                    raise RuntimeError("immutable event revision identity conflict")
                conn.execute("INSERT INTO event_revisions VALUES (?,?,?,?,?,?)", revision_values)
            elif tuple(existing_revision) != revision_values:
                raise RuntimeError("immutable event revision conflict")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def as_of(self, event_id: str, decision_time: datetime) -> dict[str, object] | None:
        _aware(decision_time)
        conn = sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT d.*,r.revision_id,r.available_at,r.text_hash,r.title,r.category FROM event_documents d JOIN event_revisions r ON r.event_id=d.event_id WHERE d.event_id=? AND d.first_seen_at<=? AND r.available_at<=? ORDER BY r.available_at DESC LIMIT 1",
                (event_id, decision_time.isoformat(), decision_time.isoformat()),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def authorize_study(
        self, protocol_hash: str, snapshot_hash: str, start: date, end: date
    ):
        receipt = OOSAccessService(self.database).authorize_known(
            protocol_hash=protocol_hash,
            snapshot_hash=snapshot_hash,
            start=start,
            end=end,
            purpose=AccessPurpose.EVENT,
        )
        if receipt.decision is not AccessDecision.ALLOW_KNOWN:
            raise PermissionError(receipt.decision.value)
        return receipt

    def record_study(
        self,
        study_id: str,
        event_id: str,
        protocol_hash: str,
        snapshot_hash: str,
        start: date,
        end: date,
        access_receipt_id: str,
        *,
        raw_return: str | None,
        excess_car: str | None,
        benchmark_id: str | None,
        status: str,
    ) -> None:
        if status == "PASS" and (
            raw_return is None or excess_car is None or not benchmark_id
        ):
            raise ValueError(
                "PASS event study requires separate raw return, excess CAR and benchmark"
            )
        conn = sqlite3.connect(self.database)
        try:
            receipt = conn.execute(
                "SELECT decision FROM access_receipts WHERE receipt_id=?",
                (access_receipt_id,),
            ).fetchone()
            provenance = conn.execute(
                "SELECT provenance FROM event_documents WHERE event_id=?", (event_id,)
            ).fetchone()
            if status == "PASS" and (
                receipt is None
                or receipt[0] != "ALLOW_KNOWN"
                or provenance is None
                or provenance[0] != "REAL"
            ):
                raise ValueError(
                    "PASS event study requires REAL event and ALLOW_KNOWN receipt"
                )
            conn.execute(
                "INSERT INTO event_study_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    study_id,
                    event_id,
                    protocol_hash,
                    snapshot_hash,
                    start.isoformat(),
                    end.isoformat(),
                    raw_return,
                    excess_car,
                    benchmark_id,
                    access_receipt_id,
                    status,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
