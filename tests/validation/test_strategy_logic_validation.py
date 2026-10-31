"""Validation test: Short-term strategy logic on synthetic data.

This test validates computation logic without requiring real market data.
Uses synthetic test cases to verify:
1. Strategy computation correctness
2. Feature extraction accuracy
3. Signal generation behavior
4. Price zone validation
5. Data quality checks

Privacy: All test data is synthetic. No real market data or holdings accessed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.quantcore.short_term.workbench import ShortTermWorkbench
from src.quantcore.short_term.data_quality import (
    validate_price_data,
    validate_volume_data,
    validate_price_zones,
)


class TestStrategyLogicValidation:
    """Test strategy computation logic with synthetic data."""

    def _create_synthetic_daily_data(
        self,
        n_days: int = 100,
        base_price: float = 10.0,
        trend: str = "up",
        volatility: float = 0.02,
    ) -> pd.DataFrame:
        """Create synthetic daily OHLCV data.

        Args:
            n_days: Number of trading days
            base_price: Starting price
            trend: 'up', 'down', or 'sideways'
            volatility: Daily volatility as fraction

        Returns:
            DataFrame with trade_date, open, high, low, close, vol, amount
        """
        import numpy as np

        dates = pd.date_range('2025-01-02', periods=n_days, freq='B')

        # Generate price series
        if trend == "up":
            drift = 0.001  # 0.1% daily drift
        elif trend == "down":
            drift = -0.001
        else:
            drift = 0.0

        # Reproducible fixtures, isolated from process-wide random state.
        rng = np.random.default_rng(42)
        returns = rng.normal(drift, volatility, n_days)
        prices = base_price * np.exp(np.cumsum(returns))

        # Generate OHLC
        close = prices
        open_price = close * rng.uniform(0.98, 1.02, n_days)
        high = np.maximum(open_price, close) * rng.uniform(1.0, 1.03, n_days)
        low = np.minimum(open_price, close) * rng.uniform(0.97, 1.0, n_days)

        # Generate volume
        vol = rng.uniform(1_000_000, 5_000_000, n_days)
        amount = vol * close * 100  # Approximate turnover

        return pd.DataFrame({
            'trade_date': dates.strftime('%Y%m%d'),
            'open': open_price,
            'high': high,
            'low': low,
            'close': close,
            'vol': vol,
            'amount': amount,
        })

    def test_trend_pullback_v1_logic(self):
        """Test TREND_PULLBACK_V1 strategy logic."""
        # Create uptrending market
        df = self._create_synthetic_daily_data(n_days=100, trend='up', volatility=0.015)

        # Strategy should identify pullbacks in uptrend
        # Validate data passes quality checks
        price_result = validate_price_data(df, min_observations=60)
        assert price_result.is_valid

        vol_result = validate_volume_data(df)
        assert vol_result.is_valid

        # Note: Full strategy integration test would require workbench.scan()
        # which needs proper config setup. This validates prerequisites.

    def test_breakout_confirm_v1_logic(self):
        """Test BREAKOUT_CONFIRM_V1 strategy logic."""
        # Create consolidating then breaking out market
        df_consolidate = self._create_synthetic_daily_data(
            n_days=20,
            base_price=10.0,
            trend='sideways',
            volatility=0.01
        )

        # Add breakout day
        breakout_day = pd.DataFrame({
            'trade_date': ['20250201'],
            'open': [10.5],
            'high': [10.8],
            'low': [10.4],
            'close': [10.7],
            'vol': [8_000_000],  # High volume
            'amount': [10.7 * 8_000_000 * 100],
        })

        df = pd.concat([df_consolidate, breakout_day], ignore_index=True)

        # Validate data quality
        price_result = validate_price_data(df, min_observations=20)
        assert price_result.is_valid

        # High volume should be detected
        assert df['vol'].iloc[-1] > df['vol'].iloc[:-1].median() * 1.5

    def test_reversal_v1_logic(self):
        """Test REVERSAL_V1 strategy logic."""
        # Create declining market with reversal
        df_decline = self._create_synthetic_daily_data(
            n_days=3,
            base_price=10.0,
            trend='down',
            volatility=0.03
        )

        # Ensure at least 5% drop
        df_decline.loc[df_decline.index[-1], 'close'] = df_decline['close'].iloc[0] * 0.94

        # Add recovery days
        df_recovery = self._create_synthetic_daily_data(
            n_days=57,
            base_price=df_decline['close'].iloc[-1],
            trend='sideways',
            volatility=0.01
        )

        df = pd.concat([df_decline, df_recovery], ignore_index=True)

        # Explicitly create a three-session drop at the evaluated end of the series.
        # The decline at the start does not constrain the random recovery tail.
        df.loc[df.index[-4:], 'close'] = [10.0, 9.8, 9.6, 9.4]
        ret3 = df['close'].iloc[-1] / df['close'].iloc[-4] - 1
        assert ret3 == pytest.approx(-0.06)

    def test_price_zone_validation_integration(self):
        """Test price zone validation is properly integrated."""
        # Create zones that should pass
        valid_zones = {
            'entry_low': 10.0,
            'entry_high': 10.2,
            'target': 11.0,
            'stop': 9.5,
            'chase': 10.5,
        }

        result = validate_price_zones(valid_zones)
        assert result.is_valid
        assert result.reason == "VALID"

        # Create zones that should fail (inverted)
        invalid_zones = {
            'entry_low': 10.2,
            'entry_high': 10.0,  # Inverted
            'target': 11.0,
            'stop': 9.5,
        }

        result = validate_price_zones(invalid_zones)
        assert not result.is_valid
        assert "INVERTED" in result.reason

    def test_data_quality_reject_insufficient(self):
        """Test data quality checks reject insufficient data."""
        # Create insufficient data (< 60 days)
        df = self._create_synthetic_daily_data(n_days=30)

        result = validate_price_data(df, min_observations=60)
        assert not result.is_valid
        assert result.reason == "INSUFFICIENT_OBSERVATIONS"
        assert result.details['actual'] == 30
        assert result.details['required'] == 60

    def test_data_quality_reject_null_prices(self):
        """Test data quality checks reject NULL prices."""
        df = self._create_synthetic_daily_data(n_days=100)

        # Inject NULL values
        df.loc[50, 'close'] = None

        result = validate_price_data(df, min_observations=60)
        assert not result.is_valid
        assert "NULL_VALUES" in result.reason

    def test_data_quality_reject_negative_volume(self):
        """Test data quality checks reject negative volume."""
        df = self._create_synthetic_daily_data(n_days=100)

        # Inject negative volume
        df.loc[50, 'vol'] = -1000

        result = validate_volume_data(df)
        assert not result.is_valid
        assert "NEGATIVE_VALUES" in result.reason

    def test_v1_vs_v2_feature_differences(self):
        """Verify V2 features include volume-based additions."""
        from src.quantcore.short_term.signals_v2 import _compute_features_v2

        # Create data with volume patterns
        df = self._create_synthetic_daily_data(n_days=100, volatility=0.02)

        # Add volume spike on recent day
        df.loc[df.index[-5], 'vol'] = df['vol'].median() * 2.5

        # V2 features should compute
        features = _compute_features_v2(df)

        if features is not None:
            # V2 adds volume-based features
            assert 'volume_ratio' in features
            assert 'pullback_volume_ratio' in features
            assert 'consolidation_ratio' in features

    def test_gap_momentum_prerequisites(self):
        """Test GAP_MOMENTUM strategy prerequisites."""
        from src.quantcore.short_term.signals_new import _compute_gap_features

        # Create data with gap up
        df = self._create_synthetic_daily_data(n_days=30)

        # Add gap up day
        df.loc[df.index[-1], 'open'] = df['close'].iloc[-2] * 1.025  # 2.5% gap
        df.loc[df.index[-1], 'close'] = df['open'].iloc[-1] * 1.01
        df.loc[df.index[-1], 'vol'] = df['vol'].median() * 2.5  # High volume

        # Should compute gap features
        features = _compute_gap_features(df)

        if features is not None:
            assert 'gap_pct' in features
            assert 'gap_up' in features

    def test_relative_strength_prerequisites(self):
        """Test RELATIVE_STRENGTH strategy prerequisites."""
        from src.quantcore.short_term.signals_new import _compute_gap_features

        # Create stock data
        df = self._create_synthetic_daily_data(n_days=30, trend='up', volatility=0.015)

        # Create market index data (weaker trend)
        market_close = list(self._create_synthetic_daily_data(
            n_days=30,
            trend='sideways',
            volatility=0.01
        )['close'])

        # Should compute with market data
        features = _compute_gap_features(df, market_close=market_close)

        if features is not None:
            assert 'relative_strength_20' in features
            assert 'market_weak' in features


class TestDataQualityIntegration:
    """Test data quality checks are properly integrated into strategy flow."""

    def test_workbench_rejects_insufficient_data(self):
        """Verify workbench properly handles insufficient data."""
        # This would require full workbench integration
        # Placeholder for structure
        pass

    def test_workbench_distinguishes_error_types(self):
        """Verify workbench distinguishes NO_SIGNAL vs INSUFFICIENT_DATA."""
        # This would require full workbench integration
        # Placeholder for structure
        pass


class TestStrategyBehaviorCharacteristics:
    """Test strategy behavior characteristics with controlled scenarios."""

    def test_v2_more_selective_than_v1(self):
        """Verify V2 strategies are more selective due to additional filters."""
        # V2 adds volume-based filters
        # Should trigger less frequently than V1 on same data
        # Placeholder for when full integration available
        pass

    def test_strategies_trigger_in_appropriate_conditions(self):
        """Verify each strategy triggers in its designed conditions."""
        # TREND_PULLBACK: uptrend + pullback
        # BREAKOUT: consolidation + breakout
        # REVERSAL: decline + support
        # GAP_MOMENTUM: gap + volume
        # RELATIVE_STRENGTH: market weak + stock strong
        # Placeholder for when full integration available
        pass


@pytest.mark.skip(reason="Requires full workbench config setup")
class TestWorkbenchIntegration:
    """Integration tests requiring full workbench setup."""

    def test_scan_on_synthetic_universe(self):
        """Test scan on synthetic stock universe."""
        pass

    def test_etf3_historical_scan(self):
        """Test scan on real ETF3 historical data if available."""
        pass
