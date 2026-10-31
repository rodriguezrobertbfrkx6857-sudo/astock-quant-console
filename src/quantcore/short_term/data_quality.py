"""Data quality validation for short-term signal generation.

This module provides strict data quality checks to prevent silent failures
and ensure signal reliability. All validations fail closed: insufficient or
invalid data returns explicit error states, not default values.

Privacy: No access to private holdings or trading data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DataQualityResult:
    """Result of data quality check."""

    is_valid: bool
    reason: str
    details: dict[str, Any]


def validate_price_data(group: Any, min_observations: int = 60) -> DataQualityResult:  # noqa: ANN401
    """Validate price data quality for signal generation.

    Args:
        group: DataFrame with trade_date, open, high, low, close columns
        min_observations: Minimum required observations (default 60)

    Returns:
        DataQualityResult with validation status
    """
    import pandas as pd  # type: ignore[import-untyped]

    details: dict[str, Any] = {}

    # Check minimum observations
    if len(group) < min_observations:
        return DataQualityResult(
            False,
            "INSUFFICIENT_OBSERVATIONS",
            {"actual": len(group), "required": min_observations}
        )

    # Check for required columns
    required_cols = {"trade_date", "open", "high", "low", "close"}
    missing = required_cols - set(group.columns)
    if missing:
        return DataQualityResult(
            False,
            "MISSING_COLUMNS",
            {"missing_columns": sorted(missing)}
        )

    # Check for duplicate dates
    dup_dates = group["trade_date"].duplicated()
    if dup_dates.any():
        dup_count = dup_dates.sum()
        details["duplicate_dates"] = int(dup_count)
        return DataQualityResult(
            False,
            "DUPLICATE_DATES",
            details
        )

    # Check for NULL/NaN in price columns
    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        null_count = pd.isna(group[col]).sum()
        if null_count > 0:
            details[f"{col}_nulls"] = int(null_count)
            return DataQualityResult(
                False,
                f"NULL_VALUES_IN_{col.upper()}",
                details
            )

    # Convert to numeric and check again
    for col in price_cols:
        numeric_col = pd.to_numeric(group[col], errors="coerce")
        if numeric_col.isna().any():
            return DataQualityResult(
                False,
                f"NON_NUMERIC_VALUES_IN_{col.upper()}",
                {"column": col}
            )

    # Check price validity (all positive)
    for col in price_cols:
        min_price = group[col].min()
        if min_price <= 0:
            return DataQualityResult(
                False,
                f"NON_POSITIVE_PRICE_IN_{col.upper()}",
                {"column": col, "min_value": float(min_price)}
            )

    # Check OHLC relationships
    invalid_ohlc = (
        (group["high"] < group["low"]) |
        (group["close"] > group["high"]) |
        (group["close"] < group["low"]) |
        (group["open"] > group["high"]) |
        (group["open"] < group["low"])
    )
    if invalid_ohlc.any():
        return DataQualityResult(
            False,
            "INVALID_OHLC_RELATIONSHIP",
            {"invalid_rows": int(invalid_ohlc.sum())}
        )

    return DataQualityResult(True, "VALID", details)


def validate_volume_data(group: Any) -> DataQualityResult:  # noqa: ANN401
    """Validate volume/amount data quality.

    Volume and amount are optional but if present must be valid.
    Missing volume/amount returns INSUFFICIENT_DATA, not a silent default.

    Args:
        group: DataFrame with optional vol/volume/amount columns

    Returns:
        DataQualityResult with validation status
    """
    import pandas as pd  # type: ignore[import-untyped]

    # Check which volume columns are present
    has_vol = "vol" in group.columns
    has_volume = "volume" in group.columns
    has_amount = "amount" in group.columns

    if not (has_vol or has_volume or has_amount):
        return DataQualityResult(
            False,
            "NO_VOLUME_DATA",
            {"available_columns": list(group.columns)}
        )

    # Validate the primary volume column
    vol_col = None
    if has_vol and not group["vol"].isna().all():
        vol_col = "vol"
    elif has_volume and not group["volume"].isna().all():
        vol_col = "volume"
    elif has_amount and not group["amount"].isna().all():
        vol_col = "amount"

    if vol_col is None:
        return DataQualityResult(
            False,
            "ALL_VOLUME_COLUMNS_NULL",
            {"checked_columns": [c for c in ["vol", "volume", "amount"] if c in group.columns]}
        )

    # Convert to numeric
    vol_numeric = pd.to_numeric(group[vol_col], errors="coerce")
    null_count = vol_numeric.isna().sum()
    if null_count > 0:
        return DataQualityResult(
            False,
            f"NULL_OR_NON_NUMERIC_IN_{vol_col.upper()}",
            {"null_count": int(null_count), "total": len(group)}
        )

    # Check for negative volume
    if (vol_numeric < 0).any():
        return DataQualityResult(
            False,
            f"NEGATIVE_VALUES_IN_{vol_col.upper()}",
            {"min_value": float(vol_numeric.min())}
        )

    # Warn if all zeros (unusual but not invalid)
    if (vol_numeric == 0).all():
        return DataQualityResult(
            False,
            "ALL_ZERO_VOLUME",
            {"column": vol_col}
        )

    return DataQualityResult(
        True,
        "VALID",
        {"volume_column": vol_col}
    )


def validate_price_zones(zones: dict[str, float | None]) -> DataQualityResult:
    """Validate price zone consistency and ordering.

    Args:
        zones: Dictionary with entry_low, entry_high, target, stop, chase

    Returns:
        DataQualityResult with validation status
    """
    details: dict[str, Any] = {}

    # Extract prices
    entry_low = zones.get("entry_low")
    entry_high = zones.get("entry_high")
    target = zones.get("target")
    stop = zones.get("stop")
    chase = zones.get("chase")

    # Check all required fields present and not None
    if entry_low is None or entry_high is None:
        return DataQualityResult(
            False,
            "MISSING_ENTRY_ZONE",
            {"entry_low": entry_low, "entry_high": entry_high}
        )

    # Check all prices are positive
    for name, price in [("entry_low", entry_low), ("entry_high", entry_high),
                         ("target", target), ("stop", stop), ("chase", chase)]:
        if price is not None and price <= 0:
            return DataQualityResult(
                False,
                f"NON_POSITIVE_{name.upper()}",
                {name: price}
            )

    # Check entry zone ordering
    if entry_low > entry_high:
        return DataQualityResult(
            False,
            "INVERTED_ENTRY_ZONE",
            {"entry_low": entry_low, "entry_high": entry_high}
        )

    # Check stop is below entry (long position risk)
    if stop is not None and stop >= entry_low:
        return DataQualityResult(
            False,
            "STOP_NOT_BELOW_ENTRY",
            {"stop": stop, "entry_low": entry_low}
        )

    # Check target is above entry
    if target is not None and target <= entry_high:
        return DataQualityResult(
            False,
            "TARGET_NOT_ABOVE_ENTRY",
            {"target": target, "entry_high": entry_high}
        )

    # Check chase threshold is reasonable
    if chase is not None and entry_high is not None:
        if chase < entry_high:
            return DataQualityResult(
                False,
                "CHASE_BELOW_ENTRY_HIGH",
                {"chase": chase, "entry_high": entry_high}
            )

    # Check zone width is reasonable (not too tight or too wide)
    zone_width = entry_high - entry_low
    zone_mid = (entry_high + entry_low) / 2
    width_pct = zone_width / zone_mid if zone_mid > 0 else 0

    if width_pct < 0.0001:  # Less than 0.01% - too tight
        return DataQualityResult(
            False,
            "ENTRY_ZONE_TOO_TIGHT",
            {"width_pct": width_pct, "entry_low": entry_low, "entry_high": entry_high}
        )

    if width_pct > 0.20:  # More than 20% - too wide
        return DataQualityResult(
            False,
            "ENTRY_ZONE_TOO_WIDE",
            {"width_pct": width_pct, "entry_low": entry_low, "entry_high": entry_high}
        )

    # All checks passed
    details["zone_width_pct"] = width_pct
    details["risk_reward_ratio"] = (target - entry_high) / (entry_low - stop) if (stop is not None and target is not None and entry_low > stop) else None

    return DataQualityResult(True, "VALID", details)


def round_price(price: float | None, tick_size: float = 0.01) -> str | None:
    """Round price to minimum tick size and return as string.

    Args:
        price: Price to round (or None)
        tick_size: Minimum price increment (default 0.01 for CNY)

    Returns:
        Formatted price string or None
    """
    if price is None:
        return None

    # Round to nearest tick
    rounded = round(price / tick_size) * tick_size

    # Format with appropriate precision
    if tick_size >= 1:
        return f"{rounded:.0f}"
    elif tick_size >= 0.01:
        return f"{rounded:.2f}"
    elif tick_size >= 0.001:
        return f"{rounded:.3f}"
    else:
        return f"{rounded:.4f}"
