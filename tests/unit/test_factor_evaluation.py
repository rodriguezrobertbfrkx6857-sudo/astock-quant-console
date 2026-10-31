"""Tests for factor-evaluation computation primitives.

Proves correctness of:
- Pearson correlation (``_pearson``) with known inputs.
- Mid-rank assignment (``_rank``) including tie handling.
- Mean helper (``_mean``).
- Factor computation for momentum, low-volatility, and reversal families.
- ``safe_float`` guards against non-finite values.
- Minimum cross-section threshold constant.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.quantcore.factors.evaluator import MIN_CROSS_SECTION, _mean, _pearson, _rank
from src.quantcore.factors.price import FactorDefinition, compute_factor, safe_float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _factor_defn(**overrides: object) -> FactorDefinition:
    """Return a minimal ``FactorDefinition`` with optional field overrides."""
    defaults = dict(
        factor_version_id="test.v1",
        factor_id="test",
        version="v1",
        family_id="PRICE_MOMENTUM",
        direction="POSITIVE",
        units="RETURN_RATIO",
        lookback=3,
        inputs=("close",),
        missing_policy="DROP",
        winsorization="NONE",
        normalization="NONE",
        neutralization="NONE",
        cost_implication="NONE",
        role="RESEARCH",
        universe="ETF3",
        available_at="CLOSE",
        evaluation_start="2020-01-01",
        evaluation_end="2020-12-31",
    )
    defaults.update(overrides)
    return FactorDefinition(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _pearson
# ---------------------------------------------------------------------------


class TestPearson:
    def test_perfect_positive(self) -> None:
        result = _pearson([Decimal("1"), Decimal("2"), Decimal("3")], [Decimal("2"), Decimal("4"), Decimal("6")])
        assert result is not None
        assert float(result) == pytest.approx(1.0)

    def test_perfect_negative(self) -> None:
        result = _pearson([Decimal("1"), Decimal("2"), Decimal("3")], [Decimal("3"), Decimal("2"), Decimal("1")])
        assert result is not None
        assert float(result) == pytest.approx(-1.0)

    def test_zero_variance_returns_none(self) -> None:
        assert _pearson([Decimal("5"), Decimal("5")], [Decimal("1"), Decimal("2")]) is None

    def test_length_mismatch_returns_none(self) -> None:
        assert _pearson([Decimal("1")], [Decimal("1"), Decimal("2")]) is None


# ---------------------------------------------------------------------------
# _rank
# ---------------------------------------------------------------------------


class TestRank:
    def test_distinct_values(self) -> None:
        assert _rank([Decimal("10"), Decimal("20"), Decimal("30")]) == [
            Decimal("1"), Decimal("2"), Decimal("3"),
        ]

    def test_tied_values_get_average_rank(self) -> None:
        # Tied at position 1-2 → average rank = (1+2)/2 = 1.5
        result = _rank([Decimal("10"), Decimal("10"), Decimal("30")])
        assert result[0] == Decimal("1.5")
        assert result[1] == Decimal("1.5")
        assert result[2] == Decimal("3")


# ---------------------------------------------------------------------------
# _mean
# ---------------------------------------------------------------------------


class TestMean:
    def test_basic_mean(self) -> None:
        result = _mean([Decimal("2"), Decimal("4")])
        assert result == Decimal("3")

    def test_empty_returns_none(self) -> None:
        assert _mean([]) is None


# ---------------------------------------------------------------------------
# compute_factor (from factors/price.py)
# ---------------------------------------------------------------------------


class TestComputeFactor:
    def test_momentum(self) -> None:
        defn = _factor_defn(family_id="PRICE_MOMENTUM", lookback=3)
        closes = [Decimal("100"), Decimal("110"), Decimal("120"), Decimal("130")]
        # 130/100 - 1 = 0.30
        assert float(compute_factor(defn, closes)) == pytest.approx(0.3)

    def test_lowvol_returns_negative_stddev(self) -> None:
        defn = _factor_defn(family_id="PRICE_LOW_VOL", lookback=3)
        closes = [Decimal("100"), Decimal("102"), Decimal("98"), Decimal("101")]
        result = compute_factor(defn, closes)
        assert result < 0  # negative volatility

    def test_reversal(self) -> None:
        defn = _factor_defn(family_id="PRICE_REVERSAL", lookback=1)
        closes = [Decimal("100"), Decimal("90")]
        # 1 - 90/100 = 0.10
        assert float(compute_factor(defn, closes)) == pytest.approx(0.1)

    def test_insufficient_lookback_raises(self) -> None:
        defn = _factor_defn(lookback=5)
        with pytest.raises(ValueError, match="not warm"):
            compute_factor(defn, [Decimal("100"), Decimal("101")])


# ---------------------------------------------------------------------------
# safe_float
# ---------------------------------------------------------------------------


class TestSafeFloat:
    def test_none_input(self) -> None:
        assert safe_float(None) is None

    def test_finite_value(self) -> None:
        assert safe_float(Decimal("3.14")) == pytest.approx(3.14)

    def test_nan_returns_none(self) -> None:
        assert safe_float(Decimal("NaN")) is None


# ---------------------------------------------------------------------------
# Minimum cross-section guard
# ---------------------------------------------------------------------------


class TestMinCrossSection:
    def test_threshold_value(self) -> None:
        """The evaluator refuses to present 3-ETF results as A-share IC."""
        assert MIN_CROSS_SECTION == 5
