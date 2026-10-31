"""Tests for data quality validation module.

Tests cover:
1. Price data validation (observations, columns, duplicates, nulls, OHLC)
2. Volume data validation (presence, nulls, negatives)
3. Price zone validation (ordering, positivity, reasonableness)
4. Price rounding

Privacy: All tests use synthetic data only.
"""

import pandas as pd
import pytest

from src.quantcore.short_term.data_quality import (
    validate_price_data,
    validate_volume_data,
    validate_price_zones,
    round_price,
)


class TestPriceDataValidation:
    """Test price data validation."""

    def test_valid_price_data(self):
        """Valid price data passes all checks."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "open": [10.0 + i * 0.1 for i in range(9)],
            "high": [10.5 + i * 0.1 for i in range(9)],
            "low": [9.5 + i * 0.1 for i in range(9)],
            "close": [10.2 + i * 0.1 for i in range(9)],
        })

        # Expand to 60 rows
        df = pd.concat([df] * 7, ignore_index=True)
        df["trade_date"] = [f"202601{i:02d}" for i in range(1, len(df) + 1)]

        result = validate_price_data(df, min_observations=60)
        assert result.is_valid
        assert result.reason == "VALID"

    def test_insufficient_observations(self):
        """Reject data with too few observations."""
        df = pd.DataFrame({
            "trade_date": ["20260101", "20260102"],
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.5, 9.6],
            "close": [10.2, 10.3],
        })

        result = validate_price_data(df, min_observations=60)
        assert not result.is_valid
        assert result.reason == "INSUFFICIENT_OBSERVATIONS"
        assert result.details["actual"] == 2
        assert result.details["required"] == 60

    def test_missing_columns(self):
        """Reject data missing required columns."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "close": [10.0 + i * 0.1 for i in range(9)],
        })

        result = validate_price_data(df, min_observations=9)
        assert not result.is_valid
        assert result.reason == "MISSING_COLUMNS"
        assert "open" in result.details["missing_columns"]

    def test_duplicate_dates(self):
        """Reject data with duplicate dates."""
        df = pd.DataFrame({
            "trade_date": ["20260101", "20260101", "20260102"],  # Duplicate
            "open": [10.0, 10.0, 10.1],
            "high": [10.5, 10.5, 10.6],
            "low": [9.5, 9.5, 9.6],
            "close": [10.2, 10.2, 10.3],
        })
        df = pd.concat([df] * 20, ignore_index=True)

        result = validate_price_data(df)
        assert not result.is_valid
        assert result.reason == "DUPLICATE_DATES"

    def test_null_values(self):
        """Reject data with NULL/NaN in price columns."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "open": [10.0, None, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8],
            "high": [10.5 + i * 0.1 for i in range(9)],
            "low": [9.5 + i * 0.1 for i in range(9)],
            "close": [10.2 + i * 0.1 for i in range(9)],
        })
        df = pd.concat([df] * 7, ignore_index=True)
        df["trade_date"] = pd.bdate_range("2026-01-01", periods=len(df)).strftime("%Y%m%d")

        result = validate_price_data(df)
        assert not result.is_valid
        assert "NULL_VALUES" in result.reason

    def test_non_positive_price(self):
        """Reject data with non-positive prices."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "open": [10.0, 10.1, 10.2, -1.0, 10.4, 10.5, 10.6, 10.7, 10.8],  # Negative
            "high": [10.5 + i * 0.1 for i in range(9)],
            "low": [9.5 + i * 0.1 for i in range(9)],
            "close": [10.2 + i * 0.1 for i in range(9)],
        })
        df = pd.concat([df] * 7, ignore_index=True)
        df["trade_date"] = pd.bdate_range("2026-01-01", periods=len(df)).strftime("%Y%m%d")

        result = validate_price_data(df)
        assert not result.is_valid
        assert "NON_POSITIVE_PRICE" in result.reason

    def test_invalid_ohlc_relationship(self):
        """Reject data with invalid OHLC relationships."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "open": [10.0 + i * 0.1 for i in range(9)],
            "high": [9.0 + i * 0.1 for i in range(9)],  # High < Low (invalid)
            "low": [9.5 + i * 0.1 for i in range(9)],
            "close": [10.2 + i * 0.1 for i in range(9)],
        })
        df = pd.concat([df] * 7, ignore_index=True)
        df["trade_date"] = pd.bdate_range("2026-01-01", periods=len(df)).strftime("%Y%m%d")

        result = validate_price_data(df)
        assert not result.is_valid
        assert result.reason == "INVALID_OHLC_RELATIONSHIP"


class TestVolumeDataValidation:
    """Test volume data validation."""

    def test_valid_volume_data(self):
        """Valid volume data passes checks."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "vol": [1000000 + i * 10000 for i in range(9)],
        })

        result = validate_volume_data(df)
        assert result.is_valid
        assert result.details["volume_column"] == "vol"

    def test_no_volume_columns(self):
        """Reject when no volume columns present."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "close": [10.0 + i * 0.1 for i in range(9)],
        })

        result = validate_volume_data(df)
        assert not result.is_valid
        assert result.reason == "NO_VOLUME_DATA"

    def test_all_volume_null(self):
        """Reject when all volume values are NULL."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "vol": [None] * 9,
        })

        result = validate_volume_data(df)
        assert not result.is_valid
        assert result.reason == "ALL_VOLUME_COLUMNS_NULL"

    def test_negative_volume(self):
        """Reject data with negative volume."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "vol": [1000000, 1100000, -500000, 1200000, 1300000, 1400000, 1500000, 1600000, 1700000],
        })

        result = validate_volume_data(df)
        assert not result.is_valid
        assert "NEGATIVE_VALUES" in result.reason

    def test_all_zero_volume(self):
        """Reject when all volume is zero (unusual)."""
        df = pd.DataFrame({
            "trade_date": [f"2026010{i}" for i in range(1, 10)],
            "vol": [0] * 9,
        })

        result = validate_volume_data(df)
        assert not result.is_valid
        assert result.reason == "ALL_ZERO_VOLUME"


class TestPriceZoneValidation:
    """Test price zone validation."""

    def test_valid_price_zones(self):
        """Valid price zones pass all checks."""
        zones = {
            "entry_low": 10.0,
            "entry_high": 10.5,
            "target": 12.0,
            "stop": 9.0,
            "chase": 11.0,
        }

        result = validate_price_zones(zones)
        assert result.is_valid
        assert result.reason == "VALID"
        assert "zone_width_pct" in result.details
        assert "risk_reward_ratio" in result.details

    def test_missing_entry_zone(self):
        """Reject when entry zone is missing."""
        zones = {
            "entry_low": None,
            "entry_high": 10.5,
            "target": 12.0,
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "MISSING_ENTRY_ZONE"

    def test_non_positive_price(self):
        """Reject zones with non-positive prices."""
        zones = {
            "entry_low": -1.0,  # Negative
            "entry_high": 10.5,
            "target": 12.0,
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert "NON_POSITIVE" in result.reason

    def test_inverted_entry_zone(self):
        """Reject when entry_low > entry_high."""
        zones = {
            "entry_low": 10.5,
            "entry_high": 10.0,  # Inverted
            "target": 12.0,
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "INVERTED_ENTRY_ZONE"

    def test_stop_not_below_entry(self):
        """Reject when stop is not below entry."""
        zones = {
            "entry_low": 10.0,
            "entry_high": 10.5,
            "target": 12.0,
            "stop": 10.2,  # Stop above entry_low
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "STOP_NOT_BELOW_ENTRY"

    def test_target_not_above_entry(self):
        """Reject when target is not above entry."""
        zones = {
            "entry_low": 10.0,
            "entry_high": 10.5,
            "target": 10.3,  # Target below entry_high
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "TARGET_NOT_ABOVE_ENTRY"

    def test_entry_zone_too_tight(self):
        """Reject when entry zone is unreasonably tight."""
        zones = {
            "entry_low": 10.0,
            "entry_high": 10.0001,  # Only 0.0001 difference
            "target": 12.0,
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "ENTRY_ZONE_TOO_TIGHT"

    def test_entry_zone_too_wide(self):
        """Reject when entry zone is unreasonably wide."""
        zones = {
            "entry_low": 10.0,
            "entry_high": 15.0,  # 50% width
            "target": 20.0,
            "stop": 9.0,
        }

        result = validate_price_zones(zones)
        assert not result.is_valid
        assert result.reason == "ENTRY_ZONE_TOO_WIDE"


class TestPriceRounding:
    """Test price rounding function."""

    def test_round_to_cent(self):
        """Round prices to 0.01 CNY."""
        assert round_price(10.123, 0.01) == "10.12"
        assert round_price(10.126, 0.01) == "10.13"
        assert round_price(10.125, 0.01) == "10.12"  # Banker's rounding

    def test_round_none(self):
        """Return None for None input."""
        assert round_price(None) is None

    def test_round_to_yuan(self):
        """Round to whole yuan."""
        assert round_price(10.6, 1.0) == "11"
        assert round_price(10.4, 1.0) == "10"
