from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from src.quantcore.universe.models import SecurityType, require_aware


@dataclass(frozen=True)
class PricePoint:
    asset_id: str
    session: date
    available_at: datetime
    close: Decimal
    volume: Decimal | None
    security_type: SecurityType

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        if self.close <= 0 or self.volume is not None and self.volume < 0:
            raise ValueError("invalid price point")


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    version: str
    family: str
    lookback: int
    direction: int
    security_types: frozenset[SecurityType]
    rebalance: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    version: str
    decision_time: datetime
    scores: dict[str, Decimal]
    targets: dict[str, Decimal]
    evidence_sessions: dict[str, date]


ETF_TYPES = frozenset(
    {
        SecurityType.ETF_DOMESTIC_EQUITY,
        SecurityType.ETF_BOND,
        SecurityType.ETF_GOLD_COMMODITY,
        SecurityType.ETF_CROSS_BORDER,
        SecurityType.ETF_MONEY,
    }
)

STRATEGY_CATALOG: dict[str, StrategyDefinition] = {
    "lowvol.v1": StrategyDefinition(
        "lowvol",
        "v1",
        "LOW_VOL",
        60,
        -1,
        frozenset({SecurityType.STOCK}),
        "MONTHLY",
        ("canonical_returns", "historical_universe"),
    ),
    "momentum.v1": StrategyDefinition(
        "momentum",
        "v1",
        "MOMENTUM",
        120,
        1,
        frozenset({SecurityType.STOCK}),
        "MONTHLY",
        ("causal_total_returns", "historical_universe"),
    ),
    "reversal.v1": StrategyDefinition(
        "reversal",
        "v1",
        "REVERSAL_LIQUIDITY",
        5,
        -1,
        frozenset({SecurityType.STOCK}),
        "WEEKLY",
        ("unadjusted_execution_prices", "past_turnover"),
    ),
    "trend.v1": StrategyDefinition(
        "trend",
        "v1",
        "TREND_DEFENSIVE",
        120,
        1,
        frozenset({SecurityType.STOCK}),
        "MONTHLY",
        ("index_signal_prices",),
    ),
    "liquidity.v1": StrategyDefinition(
        "liquidity",
        "v1",
        "LIQUIDITY",
        20,
        1,
        frozenset({SecurityType.STOCK}),
        "MONTHLY",
        ("past_turnover",),
    ),
    "etf_rotation.v1": StrategyDefinition(
        "etf_rotation",
        "v1",
        "ETF_ROTATION",
        60,
        1,
        ETF_TYPES,
        "MONTHLY",
        ("canonical_prices", "historical_etf_master"),
    ),
    "etf_lowvol.v1": StrategyDefinition(
        "etf_lowvol",
        "v1",
        "ETF_LOW_VOL",
        60,
        -1,
        ETF_TYPES,
        "MONTHLY",
        ("canonical_prices", "historical_etf_master"),
    ),
    "etf_trend_defensive.v1": StrategyDefinition(
        "etf_trend_defensive",
        "v1",
        "ETF_TREND_DEFENSIVE",
        120,
        1,
        ETF_TYPES,
        "MONTHLY",
        ("canonical_prices", "historical_etf_master"),
    ),
    "etf_momentum_20.v1": StrategyDefinition(
        "etf_momentum_20",
        "v1",
        "ETF_MOMENTUM_SHORT",
        20,
        1,
        ETF_TYPES,
        "MONTHLY",
        ("canonical_prices", "historical_etf_master"),
    ),
    "etf_risk_adjusted_60.v1": StrategyDefinition(
        "etf_risk_adjusted_60",
        "v1",
        "ETF_RISK_ADJUSTED",
        60,
        1,
        ETF_TYPES,
        "MONTHLY",
        ("canonical_prices", "historical_etf_master"),
    ),
}


def _score(definition: StrategyDefinition, points: list[PricePoint]) -> Decimal:
    prices = [float(x.close) for x in points]
    if definition.family in {"LOW_VOL", "ETF_LOW_VOL"}:
        returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
        value = statistics.pstdev(returns)
    elif definition.family in {
        "MOMENTUM",
        "REVERSAL_LIQUIDITY",
        "ETF_ROTATION",
        "ETF_MOMENTUM_SHORT",
    }:
        value = prices[-1] / prices[0] - 1
    elif definition.family == "ETF_RISK_ADJUSTED":
        returns = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
        volatility = statistics.pstdev(returns)
        value = (prices[-1] / prices[0] - 1) / volatility if volatility else 0.0
    elif definition.family in {"TREND_DEFENSIVE", "ETF_TREND_DEFENSIVE"}:
        value = prices[-1] / statistics.fmean(prices) - 1
    elif definition.family == "LIQUIDITY":
        volumes = [float(x.volume) for x in points if x.volume is not None]
        if len(volumes) != len(points):
            raise ValueError("liquidity strategy requires causal volume history")
        value = math.log1p(statistics.fmean(volumes))
    else:
        raise ValueError("unsupported strategy family")
    return Decimal(str(value * definition.direction))


def run_strategy(
    catalog_key: str,
    history: list[PricePoint],
    decision_time: datetime,
    *,
    top_n: int = 10,
) -> StrategySignal:
    cutoff = require_aware(decision_time, "decision_time")
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    definition = STRATEGY_CATALOG[catalog_key]
    grouped: dict[str, list[PricePoint]] = {}
    for point in history:
        if point.available_at > cutoff:
            continue
        if point.security_type not in definition.security_types:
            continue
        grouped.setdefault(point.asset_id, []).append(point)
    scores: dict[str, Decimal] = {}
    evidence: dict[str, date] = {}
    for asset_id, points in grouped.items():
        ordered = sorted(points, key=lambda x: (x.session, x.available_at))
        if len(ordered) < definition.lookback + 1:
            continue
        window = ordered[-(definition.lookback + 1) :]
        scores[asset_id] = _score(definition, window)
        evidence[asset_id] = window[-1].session
    selected = sorted(scores, key=lambda asset: (-scores[asset], asset))[:top_n]
    if definition.family == "ETF_TREND_DEFENSIVE" and selected:
        # A defensive trend protocol may hold cash when every ETF is below its
        # own causal moving average.  This is a distinct economic rule from
        # momentum rotation, not a renamed copy of the same signal.
        if scores[selected[0]] <= 0:
            selected = []
    weight = Decimal("1") / Decimal(len(selected)) if selected else Decimal("0")
    targets = {asset_id: weight for asset_id in selected}
    return StrategySignal(
        definition.strategy_id, definition.version, cutoff, scores, targets, evidence
    )
