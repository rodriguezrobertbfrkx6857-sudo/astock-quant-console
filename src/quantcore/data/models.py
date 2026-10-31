from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from typing import Any


class Provenance(StrEnum):
    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"
    UNKNOWN = "UNKNOWN"
    MIXED = "MIXED"


class AttemptStatus(StrEnum):
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DataLayer(StrEnum):
    RAW = "RAW"
    NORMALIZED = "NORMALIZED"
    CURATED = "CURATED"


class QualityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class Readiness(StrEnum):
    REAL_READY = "REAL_READY"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    BLOCKED = "BLOCKED"


def _require_identity(value: str, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"{label} contains unsafe characters")


def _require_aware(value: str, label: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")


@dataclass(frozen=True)
class RawAttempt:
    attempt_id: str
    source_id: str
    dataset: str
    requested_at: str
    completed_at: str
    request: dict[str, Any]
    provenance: Provenance
    status: AttemptStatus
    pages_expected: int | None
    pages_received: int
    row_count: int
    object_hashes: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt_id"),
            (self.source_id, "source_id"),
            (self.dataset, "dataset"),
        ):
            _require_identity(value, label)
        _require_aware(self.requested_at, "requested_at")
        _require_aware(self.completed_at, "completed_at")
        if self.pages_received < 0 or self.row_count < 0:
            raise ValueError("page and row counts must be non-negative")
        if self.pages_expected is not None and self.pages_expected < 0:
            raise ValueError("pages_expected must be non-negative")
        complete = (
            self.pages_expected is None or self.pages_received == self.pages_expected
        )
        if self.status is AttemptStatus.SUCCESS:
            if self.row_count <= 0 or not self.object_hashes or not complete:
                raise ValueError(
                    "SUCCESS requires non-empty content and complete pagination"
                )
        elif self.status is AttemptStatus.EMPTY:
            if self.row_count != 0 or self.object_hashes:
                raise ValueError("EMPTY cannot contain rows or data objects")
        elif self.status is AttemptStatus.PARTIAL:
            if (
                self.pages_expected is None
                or self.pages_received >= self.pages_expected
            ):
                raise ValueError("PARTIAL requires demonstrably incomplete pagination")
        elif self.status is AttemptStatus.FAILED and not (
            self.error_code or self.error_message
        ):
            raise ValueError("FAILED requires an error code or message")
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in self.object_hashes):
            raise ValueError("object hashes must be lowercase sha256")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provenance"] = self.provenance.value
        value["status"] = self.status.value
        value["object_hashes"] = list(self.object_hashes)
        return value


@dataclass(frozen=True)
class QualityFinding:
    rule: str
    passed: bool
    failure_count: int
    detail: str

    def __post_init__(self) -> None:
        if not self.rule or self.failure_count < 0:
            raise ValueError("invalid quality finding")
        if self.passed != (self.failure_count == 0):
            raise ValueError("quality finding status/count mismatch")


@dataclass(frozen=True)
class QualityReport:
    report_id: str
    status: QualityStatus
    findings: tuple[QualityFinding, ...]
    input_hash: str
    policy_version: str

    def __post_init__(self) -> None:
        expected = (
            QualityStatus.PASS
            if self.findings and all(x.passed for x in self.findings)
            else QualityStatus.FAIL
        )
        if self.status is not expected:
            raise ValueError("quality report status does not match findings")
        _require_identity(self.report_id, "report_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_hash):
            raise ValueError("quality input_hash must be sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": self.status.value,
            "findings": [asdict(item) for item in self.findings],
            "input_hash": self.input_hash,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class DataManifest:
    manifest_id: str
    dataset: str
    version: str
    layer: DataLayer
    provenance: Provenance
    source_id: str
    created_at: str
    available_at_policy: str
    schema_version: str
    normalizer_version: str | None
    build_hash: str
    object_hashes: tuple[str, ...]
    upstream_manifest_ids: tuple[str, ...]
    row_count: int
    symbol_count: int
    start_date: str | None
    end_date: str | None
    pages_expected: int | None
    pages_received: int
    attempt_status: AttemptStatus
    quality_report_id: str | None
    quality_status: QualityStatus | None
    readiness: Readiness
    reasons: tuple[str, ...] = field(default_factory=tuple)
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        for value, label in (
            (self.manifest_id, "manifest_id"),
            (self.dataset, "dataset"),
            (self.source_id, "source_id"),
            (self.version, "version"),
        ):
            _require_identity(value, label)
        _require_aware(self.created_at, "created_at")
        if self.row_count < 0 or self.symbol_count < 0 or self.pages_received < 0:
            raise ValueError("manifest counts must be non-negative")
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in self.object_hashes):
            raise ValueError("manifest object hashes must be lowercase sha256")
        if self.manifest_hash and not re.fullmatch(r"[0-9a-f]{64}", self.manifest_hash):
            raise ValueError("manifest_hash must be lowercase sha256")
        complete = (
            self.pages_expected is None or self.pages_expected == self.pages_received
        )
        if self.readiness is Readiness.REAL_READY and not (
            self.provenance is Provenance.REAL
            and self.attempt_status is AttemptStatus.SUCCESS
            and self.row_count > 0
            and complete
            and self.quality_status is QualityStatus.PASS
        ):
            raise ValueError("REAL_READY prerequisites are not satisfied")
        if (
            self.readiness is Readiness.RESEARCH_ONLY
            and self.provenance is not Provenance.SYNTHETIC
        ):
            raise ValueError(
                "RESEARCH_ONLY is reserved for isolated synthetic fixtures"
            )

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("layer", "provenance", "attempt_status", "readiness"):
            value[key] = getattr(self, key).value
        value["quality_status"] = (
            self.quality_status.value if self.quality_status else None
        )
        for key in ("object_hashes", "upstream_manifest_ids", "reasons"):
            value[key] = list(value[key])
        value.pop("manifest_hash", None)
        return value
