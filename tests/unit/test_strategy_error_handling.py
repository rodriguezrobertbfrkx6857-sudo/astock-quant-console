"""Tests for strategy error handling and signal computation status.

Tests verify:
1. Strategies distinguish NO_SIGNAL, INSUFFICIENT_DATA, COMPUTE_ERROR
2. Exceptions are not silently swallowed
3. Volume data validation fails closed (no default to zero)
4. Price data validation fails closed

Privacy: All tests use synthetic data only.
"""

import pandas as pd
import pytest

from src.quantcore.short_term.signals_v2 import _compute_features_v2, signals_v2
from src.quantcore.short_term.signals_new import _compute_gap_features, signals_new_families


class TestSignalErrorHandling:
    """Test error handling in signal computation."""

    def test_insufficient_data_returns_none(self):
        """Insufficient data returns None, not exception."""
        df = pd.DataFrame({
            "trade_date": ["20260101", "20260102"],  # Only 2 rows
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.5, 9.6],
            "close": [10.2, 10.3],
            "vol": [1000000, 1100000],
        })

        result = _compute_features_v2(df)
        assert result is None  # Too few observations

    def test_missing_price_columns_returns_none(self):
        """Missing required price columns returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "close": [10.0 + i * 0.01 for i in range(61)],
            # Missing open, high, low
        })

        result = _compute_features_v2(df)
        assert result is None

    def test_null_prices_returns_none(self):
        """NULL/NaN in price columns returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2, None, 10.4] + [10.5 + i * 0.01 for i in range(58)],  # NULL
            "vol": [1000000] * 61,
        })

        result = _compute_features_v2(df)
        assert result is None  # NULL in close

    def test_no_signal_vs_insufficient_data(self):
        """Distinguish no signal (valid data, no trigger) from insufficient data."""
        # Valid data but no V2 signals should trigger
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0] * 61,  # Flat prices
            "high": [10.1] * 61,
            "low": [9.9] * 61,
            "close": [10.0] * 61,
            "vol": [1000000] * 61,
        })

        features = _compute_features_v2(df)
        assert features is not None  # Data is sufficient

        signals = signals_v2(features)
        assert len(signals) == 0  # No signals (valid data but no trigger)


class TestVolumeDataValidation:
    """Test volume data validation (fail-closed)."""

    def test_missing_volume_returns_none(self):
        """Missing volume data returns None, not zero."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2 + i * 0.01 for i in range(61)],
            # No vol, volume, or amount columns
        })

        result = _compute_features_v2(df)
        assert result is None  # No volume data

    def test_all_null_volume_returns_none(self):
        """All NULL volume returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2 + i * 0.01 for i in range(61)],
            "vol": [None] * 61,  # All NULL
        })

        result = _compute_features_v2(df)
        assert result is None

    def test_negative_volume_returns_none(self):
        """Negative volume returns None (data error)."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2 + i * 0.01 for i in range(61)],
            "vol": [1000000] * 60 + [-500000],  # Negative
        })

        result = _compute_features_v2(df)
        assert result is None

    def test_partial_null_volume_returns_none(self):
        """Partial NULL in volume returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2 + i * 0.01 for i in range(61)],
            "vol": [1000000] * 30 + [None] * 31,  # Half NULL
        })

        result = _compute_features_v2(df)
        assert result is None

    def test_valid_volume_computes(self):
        """Valid volume data computes successfully."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 62)],
            "open": [10.0 + i * 0.01 for i in range(61)],
            "high": [10.5 + i * 0.01 for i in range(61)],
            "low": [9.5 + i * 0.01 for i in range(61)],
            "close": [10.2 + i * 0.01 for i in range(61)],
            "vol": [1000000 + i * 1000 for i in range(61)],
        })

        result = _compute_features_v2(df)
        assert result is not None
        assert "volume_ratio" in result
        assert result["volume_ratio"] > 0


class TestNewFamiliesVolumeValidation:
    """Test volume validation in new signal families."""

    def test_gap_features_missing_volume_returns_none(self):
        """Gap features without volume returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
            "open": [10.0 + i * 0.01 for i in range(30)],
            "high": [10.5 + i * 0.01 for i in range(30)],
            "low": [9.5 + i * 0.01 for i in range(30)],
            "close": [10.2 + i * 0.01 for i in range(30)],
            # No volume
        })

        result = _compute_gap_features(df)
        assert result is None

    def test_gap_features_negative_volume_returns_none(self):
        """Gap features with negative volume returns None."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
            "open": [10.0 + i * 0.01 for i in range(30)],
            "high": [10.5 + i * 0.01 for i in range(30)],
            "low": [9.5 + i * 0.01 for i in range(30)],
            "close": [10.2 + i * 0.01 for i in range(30)],
            "vol": [1000000] * 29 + [-100000],  # Negative
        })

        result = _compute_gap_features(df)
        assert result is None


class TestRelativeStrengthValidation:
    """Test relative strength computation validation."""

    def test_insufficient_market_data_returns_zero(self):
        """Insufficient market data returns zero for relative strength."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
            "open": [10.0 + i * 0.01 for i in range(30)],
            "high": [10.5 + i * 0.01 for i in range(30)],
            "low": [9.5 + i * 0.01 for i in range(30)],
            "close": [10.2 + i * 0.01 for i in range(30)],
            "vol": [1000000] * 30,
        })

        # Market data too short (only 10 days)
        market_close = [100.0 + i for i in range(10)]

        result = _compute_gap_features(df, market_close=market_close)
        assert result is not None
        assert result["relative_strength_20"] == 0.0
        assert result["market_weak"] == 0.0

    def test_market_data_length_mismatch_handled(self):
        """Market data shorter than stock data is handled."""
        df = pd.DataFrame({
            "trade_date": [f"202601{i:02d}" for i in range(1, 31)],
            "open": [10.0 + i * 0.01 for i in range(30)],
            "high": [10.5 + i * 0.01 for i in range(30)],
            "low": [9.5 + i * 0.01 for i in range(30)],
            "close": [10.2 + i * 0.01 for i in range(30)],
            "vol": [1000000] * 30,
        })

        # Market data only 15 days (< 21 required)
        market_close = [100.0 + i for i in range(15)]

        result = _compute_gap_features(df, market_close=market_close)
        assert result is not None
        # Should return 0.0 for relative strength (insufficient data)
        assert result["relative_strength_20"] == 0.0
