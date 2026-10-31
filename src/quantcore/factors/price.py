from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class FactorDefinition:
    factor_version_id: str
    factor_id: str
    version: str
    family_id: str
    direction: str
    units: str
    lookback: int
    inputs: tuple[str, ...]
    missing_policy: str
    winsorization: str
    normalization: str
    neutralization: str
    cost_implication: str
    role: str
    universe: str
    available_at: str
    evaluation_start: str
    evaluation_end: str

    @property
    def parameter_hash(self) -> str:
        body = "|".join(
            (
                self.factor_id,
                self.version,
                self.direction,
                self.units,
                str(self.lookback),
                ",".join(self.inputs),
                self.missing_policy,
                self.winsorization,
                self.normalization,
                self.neutralization,
                self.cost_implication,
                self.role,
                self.universe,
                self.available_at,
                self.evaluation_start,
                self.evaluation_end,
            )
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FactorObservation:
    on_date: date
    asset_id: str
    value: Decimal
    available_at: datetime
    forward_return: Decimal | None


FACTOR_CATALOG: dict[str, FactorDefinition] = {
    "momentum_60.v1": FactorDefinition(
        factor_version_id="momentum_60.v1",
        factor_id="momentum_60",
        version="v1",
        family_id="PRICE_MOMENTUM",
        direction="POSITIVE",
        units="RETURN_RATIO",
        lookback=60,
        inputs=("research_etf3_prices.close",),
        missing_policy="DROP_ASSET_AND_REPORT_COVERAGE",
        winsorization="NONE",
        normalization="CROSS_SECTIONAL_RANK",
        neutralization="NONE",
        cost_implication="TURNOVER_FROM_MONTHLY_REBALANCE",
        role="RESEARCH_COMPONENT",
        universe="ETF3_BOUNDED_V1",
        available_at="SESSION_CLOSE_PLUS_VENDOR_PUBLICATION",
        evaluation_start="2019-06-26",
        evaluation_end="2024-12-31",
    ),
    "lowvol_60.v1": FactorDefinition(
        factor_version_id="lowvol_60.v1",
        factor_id="lowvol_60",
        version="v1",
        family_id="PRICE_LOW_VOL",
        direction="NEGATIVE_VOLATILITY",
        units="DAILY_RETURN_STDDEV",
        lookback=60,
        inputs=("research_etf3_prices.close",),
        missing_policy="DROP_ASSET_AND_REPORT_COVERAGE",
        winsorization="NONE",
        normalization="CROSS_SECTIONAL_RANK",
        neutralization="NONE",
        cost_implication="TURNOVER_FROM_MONTHLY_REBALANCE",
        role="RESEARCH_COMPONENT",
        universe="ETF3_BOUNDED_V1",
        available_at="SESSION_CLOSE_PLUS_VENDOR_PUBLICATION",
        evaluation_start="2019-06-26",
        evaluation_end="2024-12-31",
    ),
}


def _large_price_factor(
    factor_id: str,
    family_id: str,
    lookback: int,
    *,
    direction: str,
    units: str,
    inputs: tuple[str, ...] = ("A_SHARE_PRICE_ONLY.daily.close",),
) -> FactorDefinition:
    return FactorDefinition(
        factor_version_id=f"{factor_id}.v2",
        factor_id=factor_id,
        version="v2",
        family_id=family_id,
        direction=direction,
        units=units,
        lookback=lookback,
        inputs=inputs,
        missing_policy="DROP_ASSET_DATE_AND_REPORT_COVERAGE",
        winsorization="CROSS_SECTIONAL_1_99_PERCENTILE",
        normalization="CROSS_SECTIONAL_RANK",
        neutralization="NONE_PRICE_ONLY_V1",
        cost_implication="REPORT_TOP_QUINTILE_TURNOVER",
        role="RESEARCH_COMPONENT",
        universe="A_SHARE_PRICE_ONLY_V1",
        available_at="CONSERVATIVE_EOD_17_00_ASIA_SHANGHAI",
        evaluation_start="2025-01-02",
        evaluation_end="2025-08-18",
    )


FACTOR_CATALOG.update(
    {
        definition.factor_version_id: definition
        for definition in (
            _large_price_factor("momentum_20", "PRICE_MOMENTUM", 20, direction="POSITIVE", units="RETURN_RATIO"),
            _large_price_factor("momentum_60_a_share", "PRICE_MOMENTUM", 60, direction="POSITIVE", units="RETURN_RATIO"),
            _large_price_factor("momentum_120", "PRICE_MOMENTUM", 120, direction="POSITIVE", units="RETURN_RATIO"),
            _large_price_factor("reversal_5", "PRICE_REVERSAL", 5, direction="NEGATIVE_RECENT_RETURN", units="RETURN_RATIO"),
            _large_price_factor("reversal_20", "PRICE_REVERSAL", 20, direction="NEGATIVE_RECENT_RETURN", units="RETURN_RATIO"),
            _large_price_factor("lowvol_20", "PRICE_LOW_VOL", 20, direction="NEGATIVE_VOLATILITY", units="DAILY_RETURN_STDDEV"),
            _large_price_factor("lowvol_60_a_share", "PRICE_LOW_VOL", 60, direction="NEGATIVE_VOLATILITY", units="DAILY_RETURN_STDDEV"),
            _large_price_factor("trend_strength_60", "PRICE_TREND_STRENGTH", 60, direction="POSITIVE", units="RETURN_PER_VOLATILITY"),
            _large_price_factor(
                "turnover_20",
                "TURNOVER_RATE",
                20,
                direction="DIAGNOSTIC",
                units="TURNOVER_RATE",
                inputs=("A_SHARE_DAILY_BASIC.turnover_rate",),
            ),
            _large_price_factor(
                "liquidity_20",
                "PRICE_LIQUIDITY",
                20,
                direction="POSITIVE",
                units="LOG_AVERAGE_AMOUNT",
                inputs=("A_SHARE_PRICE_ONLY.daily.amount",),
            ),
            _large_price_factor(
                "volume_surprise_20",
                "VOLUME_SURPRISE",
                20,
                direction="POSITIVE",
                units="VOLUME_RATIO",
                inputs=("A_SHARE_PRICE_ONLY.daily.vol",),
            ),
            _large_price_factor(
                "beta_60",
                "BETA",
                60,
                direction="DIAGNOSTIC",
                units="BETA",
                inputs=("A_SHARE_PRICE_ONLY.daily.close", "HS300_PRICE_ONLY.close"),
            ),
            _large_price_factor(
                "idio_vol_60",
                "IDIOSYNCRATIC_VOLATILITY",
                60,
                direction="NEGATIVE_VOLATILITY",
                units="DAILY_RETURN_STDDEV",
                inputs=("A_SHARE_PRICE_ONLY.daily.close", "HS300_PRICE_ONLY.close"),
            ),
        )
    }
)


def _returns(points: Iterable[Decimal]) -> list[Decimal]:
    values = list(points)
    return [values[index] / values[index - 1] - Decimal(1) for index in range(1, len(values))]


def compute_factor(definition: FactorDefinition, closes: list[Decimal]) -> Decimal:
    if len(closes) < definition.lookback + 1:
        raise ValueError("factor lookback is not warm")
    window = closes[-(definition.lookback + 1) :]
    if definition.family_id == "PRICE_MOMENTUM":
        return window[-1] / window[0] - Decimal(1)
    if definition.family_id == "PRICE_LOW_VOL":
        values = [float(x) for x in _returns(window)]
        return Decimal(str(-statistics.pstdev(values)))
    if definition.family_id == "PRICE_REVERSAL":
        return Decimal(1) - window[-1] / window[0]
    if definition.family_id == "PRICE_TREND_STRENGTH":
        values = [float(x) for x in _returns(window)]
        volatility = statistics.pstdev(values)
        if volatility == 0:
            raise ValueError("trend strength volatility is zero")
        return (window[-1] / window[0] - Decimal(1)) / Decimal(str(volatility))
    raise ValueError(f"unsupported factor family: {definition.family_id}")


def factor_source_hash() -> str:
    """Hash this implementation so a registry row binds to real source."""

    import pathlib

    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


def safe_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None
