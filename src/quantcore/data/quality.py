from __future__ import annotations

from uuid import uuid4

from .hashing import canonical_hash
from .models import QualityFinding, QualityReport, QualityStatus


def evaluate_records(
    records: list[dict[str, object]],
    *,
    required_fields: tuple[str, ...],
    unique_fields: tuple[str, ...],
    policy_version: str = "s01.v1",
) -> QualityReport:
    missing = sum(
        1 for row in records if any(row.get(field) is None for field in required_fields)
    )
    keys = [tuple(row.get(field) for field in unique_fields) for row in records]
    duplicates = len(keys) - len(set(keys))
    findings = (
        QualityFinding(
            "non_empty", bool(records), 0 if records else 1, "dataset must contain rows"
        ),
        QualityFinding(
            "required_fields", missing == 0, missing, ",".join(required_fields)
        ),
        QualityFinding(
            "unique_key", duplicates == 0, duplicates, ",".join(unique_fields)
        ),
    )
    status = (
        QualityStatus.PASS
        if all(item.passed for item in findings)
        else QualityStatus.FAIL
    )
    return QualityReport(
        f"quality-{uuid4().hex}",
        status,
        findings,
        canonical_hash(records),
        policy_version,
    )
