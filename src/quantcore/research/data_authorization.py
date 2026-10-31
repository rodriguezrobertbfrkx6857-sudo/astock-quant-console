"""Data authorization and eligibility checking for strategy research.

This module defines three authorization levels and checks strategy data requirements:
1. display_authorized - Can show current prices
2. research_authorized - Can perform historical backtesting
3. execution_eligible - Can generate executable signals

Key principles:
- Fail closed: missing data blocks authorization, not defaults to allowed
- Explicit blockers: record precise reasons for denial
- No disguising: PRICE_ONLY cannot pretend to be research_authorized
- PIT requirements: current stock pool cannot masquerade as historical pool

Privacy: This module does not access private holdings or trading data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AuthorizationLevel(StrEnum):
    """Data authorization levels for strategies."""

    DISPLAY_ONLY = "DISPLAY_ONLY"  # Can show current prices only
    OBSERVATION_ONLY = "OBSERVATION_ONLY"  # Engineering observation, not validated
    RESEARCH_AUTHORIZED = "RESEARCH_AUTHORIZED"  # Can perform historical validation
    EXECUTION_ELIGIBLE = "EXECUTION_ELIGIBLE"  # Can generate executable signals


class DataField(StrEnum):
    """Required data fields for strategies."""

    # Price fields
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"

    # Volume fields
    VOL = "vol"  # Volume in shares or lots
    AMOUNT = "amount"  # Turnover amount in CNY

    # Corporate action fields
    ADJ_FACTOR = "adj_factor"  # Adjustment factor for splits/dividends

    # Trading state fields
    IS_SUSPENDED = "is_suspended"  # Suspension status
    UP_LIMIT = "up_limit"  # Upper price limit
    DOWN_LIMIT = "down_limit"  # Lower price limit
    IS_ST = "is_st"  # Special treatment status

    # Identity fields
    STOCK_BASIC = "stock_basic"  # Current stock basic info
    HISTORICAL_UNIVERSE = "historical_universe"  # Historical stock pool state (PIT)

    # Index fields
    MARKET_INDEX = "market_index"  # Market index prices (for relative strength)

    # Intraday fields
    MINUTE_BARS = "minute_bars"  # Minute-level OHLCV
    MINUTE_ORDER = "minute_order"  # Within-minute price ordering


@dataclass(frozen=True)
class DataDependency:
    """Data dependency specification for a strategy."""

    strategy_family: str
    required_fields: tuple[DataField, ...]
    optional_fields: tuple[DataField, ...]
    min_observations: int
    requires_pit_universe: bool
    min_authorization_level: AuthorizationLevel


@dataclass(frozen=True)
class DataBlocker:
    """Reason why a strategy is blocked from authorization."""

    strategy_family: str
    requested_level: AuthorizationLevel
    primary_reason: str
    missing_fields: tuple[DataField, ...]
    has_survivorship_bias_risk: bool
    has_future_leakage_risk: bool
    mitigation_options: tuple[str, ...]
    details: dict[str, Any]


@dataclass(frozen=True)
class AuthorizationResult:
    """Result of authorization check."""

    strategy_family: str
    requested_level: AuthorizationLevel
    is_authorized: bool
    granted_level: AuthorizationLevel
    blocker: DataBlocker | None


# Strategy data dependencies registry
STRATEGY_DATA_DEPENDENCIES: dict[str, DataDependency] = {
    # ── ETF Strategies (fully authorized) ──────────────────────────────
    "ETF_ROTATION": DataDependency(
        strategy_family="ETF_ROTATION",
        required_fields=(
            DataField.CLOSE,
            DataField.VOL,
            DataField.AMOUNT,
        ),
        optional_fields=(DataField.ADJ_FACTOR,),
        min_observations=60,
        requires_pit_universe=False,  # Small fixed pool
        min_authorization_level=AuthorizationLevel.RESEARCH_AUTHORIZED,
    ),
    "ETF_LOWVOL": DataDependency(
        strategy_family="ETF_LOWVOL",
        required_fields=(
            DataField.CLOSE,
            DataField.VOL,
            DataField.AMOUNT,
        ),
        optional_fields=(DataField.ADJ_FACTOR,),
        min_observations=60,
        requires_pit_universe=False,
        min_authorization_level=AuthorizationLevel.RESEARCH_AUTHORIZED,
    ),
    # ── Short-term V1 Strategies ───────────────────────────────────────
    "SHORT_TREND_PULLBACK_V1": DataDependency(
        strategy_family="SHORT_TREND_PULLBACK_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,  # Can substitute for amount in observation mode
        ),
        optional_fields=(
            DataField.AMOUNT,  # Preferred for liquidity filter
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,  # For historical validation
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    "SHORT_BREAKOUT_CONFIRM_V1": DataDependency(
        strategy_family="SHORT_BREAKOUT_CONFIRM_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.AMOUNT,  # Required for volume confirmation (cannot substitute)
        ),
        optional_fields=(
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    "SHORT_REVERSAL_V1": DataDependency(
        strategy_family="SHORT_REVERSAL_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.AMOUNT,  # Required for volume confirmation
        ),
        optional_fields=(
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    # ── Short-term V2 Strategies (stricter volume requirements) ────────
    "SHORT_TREND_PULLBACK_V2": DataDependency(
        strategy_family="SHORT_TREND_PULLBACK_V2",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,  # V2: volume features required
        ),
        optional_fields=(
            DataField.AMOUNT,
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    "SHORT_BREAKOUT_CONFIRM_V2": DataDependency(
        strategy_family="SHORT_BREAKOUT_CONFIRM_V2",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,
            DataField.AMOUNT,  # V2: consolidation + volume
        ),
        optional_fields=(
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    "SHORT_REVERSAL_V2": DataDependency(
        strategy_family="SHORT_REVERSAL_V2",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,  # V2: volume climax required
            DataField.AMOUNT,
        ),
        optional_fields=(
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=60,
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    # ── New Strategy Families ──────────────────────────────────────────
    "SHORT_GAP_MOMENTUM_V1": DataDependency(
        strategy_family="SHORT_GAP_MOMENTUM_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,  # High volume required (>= 2x median)
        ),
        optional_fields=(
            DataField.AMOUNT,
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=30,  # Shorter lookback
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    "SHORT_RELATIVE_STRENGTH_V1": DataDependency(
        strategy_family="SHORT_RELATIVE_STRENGTH_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.VOL,
            DataField.MARKET_INDEX,  # Market index for relative strength
        ),
        optional_fields=(
            DataField.AMOUNT,
            DataField.ADJ_FACTOR,
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        min_observations=21,  # Need 20-day lookback
        requires_pit_universe=True,
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    # ── Holding Management ─────────────────────────────────────────────
    "HOLDING_NEXT_DAY_V1": DataDependency(
        strategy_family="HOLDING_NEXT_DAY_V1",
        required_fields=(
            DataField.OPEN,
            DataField.HIGH,
            DataField.LOW,
            DataField.CLOSE,
            DataField.IS_SUSPENDED,  # Required for executable status
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        optional_fields=(
            DataField.VOL,
            DataField.AMOUNT,
            DataField.ADJ_FACTOR,
        ),
        min_observations=60,
        requires_pit_universe=False,  # Uses manual holdings
        min_authorization_level=AuthorizationLevel.OBSERVATION_ONLY,
    ),
    # ── Intraday T-trading ─────────────────────────────────────────────
    "BASE_T_CONDITIONAL_V1": DataDependency(
        strategy_family="BASE_T_CONDITIONAL_V1",
        required_fields=(
            DataField.MINUTE_BARS,  # Minute-level OHLCV required
            DataField.MINUTE_ORDER,  # Within-minute ordering
            DataField.IS_SUSPENDED,
            DataField.UP_LIMIT,
            DataField.DOWN_LIMIT,
        ),
        optional_fields=(
            DataField.ADJ_FACTOR,
        ),
        min_observations=1,  # Intraday strategy
        requires_pit_universe=False,
        min_authorization_level=AuthorizationLevel.EXECUTION_ELIGIBLE,
    ),
}


def check_authorization(
    strategy_family: str,
    available_fields: set[DataField],
    has_pit_universe: bool,
    requested_level: AuthorizationLevel,
) -> AuthorizationResult:
    """Check if a strategy can be authorized at requested level.

    Args:
        strategy_family: Strategy family identifier
        available_fields: Set of available data fields
        has_pit_universe: Whether historical PIT universe is available
        requested_level: Requested authorization level

    Returns:
        AuthorizationResult with granted level and blockers
    """
    dependency = STRATEGY_DATA_DEPENDENCIES.get(strategy_family)
    if dependency is None:
        # Unknown strategy - deny
        return AuthorizationResult(
            strategy_family=strategy_family,
            requested_level=requested_level,
            is_authorized=False,
            granted_level=AuthorizationLevel.DISPLAY_ONLY,
            blocker=DataBlocker(
                strategy_family=strategy_family,
                requested_level=requested_level,
                primary_reason="UNKNOWN_STRATEGY_FAMILY",
                missing_fields=(),
                has_survivorship_bias_risk=False,
                has_future_leakage_risk=False,
                mitigation_options=(),
                details={"message": "Strategy family not registered"},
            ),
        )

    # Check required fields
    missing_required = set(dependency.required_fields) - available_fields
    if missing_required:
        return AuthorizationResult(
            strategy_family=strategy_family,
            requested_level=requested_level,
            is_authorized=False,
            granted_level=AuthorizationLevel.DISPLAY_ONLY,
            blocker=DataBlocker(
                strategy_family=strategy_family,
                requested_level=requested_level,
                primary_reason="MISSING_REQUIRED_FIELDS",
                missing_fields=tuple(missing_required),
                has_survivorship_bias_risk=False,
                has_future_leakage_risk=False,
                mitigation_options=_suggest_mitigations(missing_required),
                details={"required_fields": list(dependency.required_fields)},
            ),
        )

    # Check PIT universe requirement for research
    if requested_level in (
        AuthorizationLevel.RESEARCH_AUTHORIZED,
        AuthorizationLevel.EXECUTION_ELIGIBLE,
    ):
        if dependency.requires_pit_universe and not has_pit_universe:
            return AuthorizationResult(
                strategy_family=strategy_family,
                requested_level=requested_level,
                is_authorized=False,
                granted_level=AuthorizationLevel.OBSERVATION_ONLY,
                blocker=DataBlocker(
                    strategy_family=strategy_family,
                    requested_level=requested_level,
                    primary_reason="HISTORICAL_PIT_UNIVERSE_REQUIRED",
                    missing_fields=(DataField.HISTORICAL_UNIVERSE,),
                    has_survivorship_bias_risk=True,
                    has_future_leakage_risk=False,
                    mitigation_options=(
                        "Accept OBSERVATION_ONLY with survivorship bias risk",
                        "Obtain certified historical universe data",
                        "Use BEST_EFFORT_LEGACY archive (if available)",
                    ),
                    details={
                        "message": "Historical point-in-time universe required to avoid survivorship bias",
                        "current_stock_basic_not_sufficient": True,
                    },
                ),
            )

    # Check minimum authorization level
    if requested_level < dependency.min_authorization_level:
        # Requested level is too low for this strategy
        return AuthorizationResult(
            strategy_family=strategy_family,
            requested_level=requested_level,
            is_authorized=False,
            granted_level=requested_level,
            blocker=DataBlocker(
                strategy_family=strategy_family,
                requested_level=requested_level,
                primary_reason="REQUESTED_LEVEL_TOO_LOW",
                missing_fields=(),
                has_survivorship_bias_risk=False,
                has_future_leakage_risk=False,
                mitigation_options=(),
                details={
                    "min_required_level": dependency.min_authorization_level.value,
                    "message": f"Strategy requires at least {dependency.min_authorization_level.value}",
                },
            ),
        )

    # All checks passed
    return AuthorizationResult(
        strategy_family=strategy_family,
        requested_level=requested_level,
        is_authorized=True,
        granted_level=requested_level,
        blocker=None,
    )


def _suggest_mitigations(missing_fields: set[DataField]) -> tuple[str, ...]:
    """Suggest mitigation options for missing fields."""
    mitigations = []

    if DataField.AMOUNT in missing_fields:
        mitigations.append("Use vol (volume in shares/lots) as proxy for amount")
        mitigations.append("Obtain amount field from data provider")

    if DataField.HISTORICAL_UNIVERSE in missing_fields:
        mitigations.append("Accept OBSERVATION_ONLY mode")
        mitigations.append("Use BEST_EFFORT_LEGACY archive")
        mitigations.append("Obtain certified historical universe data")

    if DataField.ADJ_FACTOR in missing_fields:
        mitigations.append("Use unadjusted prices (mark results as 'excluding dividends')")
        mitigations.append("Obtain adjustment factor data")

    if DataField.IS_SUSPENDED in missing_fields:
        mitigations.append("Mark executable status as UNKNOWN")
        mitigations.append("Obtain suspension data from suspend_d")

    if DataField.MINUTE_BARS in missing_fields:
        mitigations.append("Complete WATCH vendor acceptance")
        mitigations.append("Use daily-only strategies")

    if not mitigations:
        mitigations.append("Obtain missing data fields from provider")

    return tuple(mitigations)


def generate_blocker_report(
    strategy_families: list[str],
    available_fields: set[DataField],
    has_pit_universe: bool,
    requested_level: AuthorizationLevel = AuthorizationLevel.RESEARCH_AUTHORIZED,
) -> dict[str, Any]:
    """Generate comprehensive blocker report for multiple strategies.

    Args:
        strategy_families: List of strategy family identifiers
        available_fields: Set of currently available data fields
        has_pit_universe: Whether historical PIT universe is available
        requested_level: Requested authorization level

    Returns:
        Structured blocker report
    """
    results = []
    blocked_count = 0
    authorized_count = 0

    for family in strategy_families:
        result = check_authorization(
            family,
            available_fields,
            has_pit_universe,
            requested_level,
        )
        results.append(result)

        if result.is_authorized:
            authorized_count += 1
        else:
            blocked_count += 1

    return {
        "requested_level": requested_level.value,
        "total_strategies": len(strategy_families),
        "authorized_count": authorized_count,
        "blocked_count": blocked_count,
        "results": [
            {
                "strategy_family": r.strategy_family,
                "is_authorized": r.is_authorized,
                "granted_level": r.granted_level.value,
                "blocker": (
                    {
                        "primary_reason": r.blocker.primary_reason,
                        "missing_fields": [f.value for f in r.blocker.missing_fields],
                        "has_survivorship_bias_risk": r.blocker.has_survivorship_bias_risk,
                        "mitigation_options": list(r.blocker.mitigation_options),
                        "details": r.blocker.details,
                    }
                    if r.blocker
                    else None
                ),
            }
            for r in results
        ],
    }
