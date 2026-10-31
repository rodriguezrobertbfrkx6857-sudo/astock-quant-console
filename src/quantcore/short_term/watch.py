"""Fail-closed Watch gate for plan-linked intraday observations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WatchDecision:
    state: str
    reason: str
    actionable: bool


def receipt_key(plan_id: str, trigger_type: str, source_timestamp: str) -> str:
    return hashlib.sha256(f"{plan_id}|{trigger_type}|{source_timestamp}".encode()).hexdigest()


def assess_observation(
    *,
    source_timestamp: datetime,
    observed_at: datetime,
    previous_source_timestamp: datetime | None,
    full_acceptance: bool,
    freshness_seconds: int = 90,
) -> WatchDecision:
    if not full_acceptance:
        return WatchDecision("BLOCKED_UNVERIFIED", "full Watch acceptance has not passed", False)
    age = (observed_at - source_timestamp).total_seconds()
    if age < -5 or age > freshness_seconds:
        return WatchDecision("BLOCKED_STALE", f"source age {age:.3f}s exceeds the frozen freshness gate", False)
    if previous_source_timestamp is not None and source_timestamp <= previous_source_timestamp:
        return WatchDecision("BLOCKED_OUT_OF_ORDER", "duplicate or out-of-order source timestamp", False)
    return WatchDecision("OBSERVED", "fresh monotonic observation may be evaluated against the frozen plan", True)
