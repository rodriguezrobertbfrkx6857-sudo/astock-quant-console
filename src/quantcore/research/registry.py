from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Iterable

from src.quantcore.data.models import Readiness
from src.quantcore.research.oos import AccessDecision, AccessReceipt


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ResearchProtocol:
    protocol_id: str
    family_id: str
    hypothesis: str
    falsifier: str
    source_code_hash: str
    parameter_grid: tuple[tuple[str, tuple[object, ...]], ...]
    direction: str
    universe_version: str
    data_dependencies: tuple[str, ...]
    availability_policy: str
    rebalance_and_execution: str
    holding_constraints: str
    cost_model: str
    train_window: tuple[str, str]
    validation_windows: tuple[tuple[str, str], ...]
    label_horizon: int
    purge_sessions: int
    embargo_sessions: int
    controls: tuple[str, ...]
    primary_metric: str
    statistics_policy: str
    family_budget: int
    stop_rule: str
    oos_access: str
    publication_level: str
    owner_version: str
    candidate_threshold: Decimal
    control_only: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.protocol_id):
            raise ValueError("invalid protocol_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_code_hash):
            raise ValueError("source_code_hash must be sha256")
        required = (
            self.hypothesis,
            self.falsifier,
            self.direction,
            self.universe_version,
            self.availability_policy,
            self.rebalance_and_execution,
            self.holding_constraints,
            self.cost_model,
            self.primary_metric,
            self.statistics_policy,
            self.stop_rule,
            self.publication_level,
            self.owner_version,
        )
        if not all(required):
            raise ValueError("protocol fields may not be blank")
        if self.oos_access != "KNOWN_ONLY":
            raise ValueError("Integrated V2 construction only permits KNOWN_ONLY")
        if not 1 <= self.family_budget <= 6:
            raise ValueError("family budget must be in 1..6")
        config_count = 1
        for _, values in self.parameter_grid:
            config_count *= len(values)
        if config_count > self.family_budget:
            raise ValueError("parameter grid exceeds frozen family budget")
        if len(self.validation_windows) < 3:
            raise ValueError("at least three validation windows are required")
        if (
            self.label_horizon < 0
            or self.purge_sessions < self.label_horizon
            or self.embargo_sessions < 0
        ):
            raise ValueError("purge must cover the label horizon")

    @property
    def protocol_hash(self) -> str:
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class StrategyVersion:
    strategy_version_id: str
    family_id: str
    protocol_hash: str
    source_code_hash: str
    snapshot_hash: str
    universe_version: str

    def __post_init__(self) -> None:
        for value in (self.protocol_hash, self.source_code_hash, self.snapshot_hash):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("version bindings require sha256 values")


class QueueStatus(StrEnum):
    PLANNED = "PLANNED"
    BLOCKED_DATA = "BLOCKED_DATA"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Stage09Rating(StrEnum):
    INVALID = "INVALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REJECT = "REJECT"
    RESEARCH = "RESEARCH"
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class Stage09Result:
    rating: Stage09Rating
    reasons: tuple[str, ...]
    protocol_hash: str
    snapshot_hash: str
    primary_metric: Decimal | None
    effective_trades: int
    validation_blocks: int

    @property
    def paper_eligible(self) -> bool:
        return False


def evaluate_stage09(
    protocol: ResearchProtocol,
    version: StrategyVersion,
    *,
    readiness: Readiness,
    access_receipt: AccessReceipt,
    effective_trades: int,
    validation_blocks: int,
    primary_metric: Decimal | None,
    future_leakage: bool = False,
    snapshot_hash: str | None = None,
) -> Stage09Result:
    """Evaluate a frozen run against the Stage09 gates.

    ``StrategyVersion.snapshot_hash`` is retained as the backwards-compatible
    default.  A real executor may pass the immutable input snapshot it built
    from the published manifests; that is the hash which must be persisted in
    run evidence and used for later qualification.
    """
    result_snapshot_hash = snapshot_hash or version.snapshot_hash
    reasons: list[str] = []
    if (
        version.protocol_hash != protocol.protocol_hash
        or version.source_code_hash != protocol.source_code_hash
        or version.universe_version != protocol.universe_version
    ):
        reasons.append("VERSION_BINDING_MISMATCH")
    if readiness is not Readiness.REAL_READY:
        reasons.append("DATA_NOT_REAL_READY")
    if access_receipt.decision is not AccessDecision.ALLOW_KNOWN:
        reasons.append("OOS_ACCESS_DENIED")
    if future_leakage:
        reasons.append("FUTURE_LEAKAGE")
    if reasons:
        return Stage09Result(
            Stage09Rating.INVALID,
            tuple(reasons),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    if validation_blocks < 3 or primary_metric is None:
        return Stage09Result(
            Stage09Rating.INSUFFICIENT_DATA,
            ("INSUFFICIENT_VALIDATION",),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    if protocol.control_only:
        return Stage09Result(
            Stage09Rating.REJECT,
            ("CONTROL_NEVER_PROMOTES",),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    if effective_trades <= 0:
        return Stage09Result(
            Stage09Rating.REJECT,
            ("ZERO_EFFECTIVE_TRADES",),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    if primary_metric >= protocol.candidate_threshold:
        return Stage09Result(
            Stage09Rating.CANDIDATE,
            ("FROZEN_THRESHOLD_MET",),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    if primary_metric < Decimal("0"):
        return Stage09Result(
            Stage09Rating.REJECT,
            ("PRIMARY_METRIC_NEGATIVE",),
            protocol.protocol_hash,
            result_snapshot_hash,
            primary_metric,
            effective_trades,
            validation_blocks,
        )
    return Stage09Result(
        Stage09Rating.RESEARCH,
        ("BELOW_CANDIDATE_THRESHOLD",),
        protocol.protocol_hash,
        result_snapshot_hash,
        primary_metric,
        effective_trades,
        validation_blocks,
    )


@dataclass(frozen=True)
class PurgedFold:
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]


def build_purged_folds(
    sample_count: int, validation_blocks: int, label_horizon: int, embargo: int = 0
) -> tuple[PurgedFold, ...]:
    if (
        validation_blocks < 3
        or sample_count < validation_blocks * 2
        or label_horizon < 0
        or embargo < 0
    ):
        raise ValueError("invalid fold design")
    block = sample_count // (validation_blocks + 1)
    folds: list[PurgedFold] = []
    for fold_no in range(validation_blocks):
        start = block * (fold_no + 1)
        end = sample_count if fold_no == validation_blocks - 1 else start + block
        train_end = max(0, start - label_horizon)
        train = tuple(range(train_end))
        validation = tuple(range(start, max(start, end - embargo)))
        if not train or not validation:
            raise ValueError("purge leaves an empty fold")
        folds.append(PurgedFold(train, validation))
    return tuple(folds)


def run_isolated_folds(
    folds: Iterable[PurgedFold],
    strategy_factory: Callable[[], object],
    run_fold: Callable[[object, PurgedFold], object],
) -> tuple[object, ...]:
    states: list[object] = []
    results: list[object] = []
    for fold in folds:
        state = strategy_factory()
        if any(state is prior for prior in states):
            raise RuntimeError("strategy_factory reused mutable state across folds")
        states.append(state)
        results.append(run_fold(state, fold))
    return tuple(results)
