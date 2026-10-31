from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.quantcore.universe.models import Confidence, require_aware


@dataclass(frozen=True)
class FinancialRevision:
    asset_id: str
    statement: str
    report_period: date
    revision_id: str
    available_at: datetime
    observed_at: datetime
    source_ref: str
    source_row_hash: str
    confidence: Confidence
    values: dict[str, Any]

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        require_aware(self.observed_at, "observed_at")
        if not all(
            (
                self.asset_id,
                self.statement,
                self.revision_id,
                self.source_ref,
                self.source_row_hash,
            )
        ):
            raise ValueError("financial identity, source, and revision are required")


class FinancialRevisionStore:
    def __init__(self, revisions: list[FinancialRevision]):
        keys = [
            (x.asset_id, x.statement, x.report_period, x.revision_id) for x in revisions
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate financial revision")
        self._revisions = tuple(revisions)

    def as_of(
        self,
        decision_time: datetime,
        *,
        asset_ids: set[str] | None = None,
        statements: set[str] | None = None,
        strict: bool = True,
    ) -> list[FinancialRevision]:
        cutoff = require_aware(decision_time, "decision_time")
        eligible = [
            item
            for item in self._revisions
            if item.available_at <= cutoff
            and (not asset_ids or item.asset_id in asset_ids)
            and (not statements or item.statement in statements)
            and (not strict or item.confidence is Confidence.VERIFIED)
        ]
        latest: dict[tuple[str, str, date], FinancialRevision] = {}
        for item in sorted(
            eligible, key=lambda x: (x.available_at, x.observed_at, x.revision_id)
        ):
            latest[(item.asset_id, item.statement, item.report_period)] = item
        return sorted(
            latest.values(), key=lambda x: (x.asset_id, x.statement, x.report_period)
        )
