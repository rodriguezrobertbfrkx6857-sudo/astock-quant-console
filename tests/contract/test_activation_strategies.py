from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from src.quantcore.strategies import STRATEGY_CATALOG, PricePoint, run_strategy
from src.quantcore.universe.models import SecurityType


class ActivationStrategyTests(unittest.TestCase):
    def test_price_strategies_are_executable_and_pit_filtered(self) -> None:
        self.assertGreaterEqual(len(STRATEGY_CATALOG), 8)
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        history = []
        for index in range(122):
            history.append(
                PricePoint(
                    "A",
                    date(2025, 1, 1) + timedelta(days=index),
                    cutoff - timedelta(days=122 - index),
                    Decimal(10 + index) / Decimal(10),
                    Decimal(100 + index),
                    SecurityType.STOCK,
                )
            )
        history.append(
            PricePoint(
                "FUTURE",
                date(2026, 1, 2),
                cutoff + timedelta(seconds=1),
                Decimal("9"),
                Decimal("1"),
                SecurityType.STOCK,
            )
        )
        signal = run_strategy("momentum.v1", history, cutoff, top_n=1)
        self.assertEqual(signal.targets, {"A": Decimal("1")})
        self.assertNotIn("FUTURE", signal.scores)

    def test_generic_unclassified_etf_is_fail_closed(self) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        history = [
            PricePoint(
                "ETF",
                date(2025, 1, 1) + timedelta(days=index),
                cutoff - timedelta(days=70 - index),
                Decimal(10 + index),
                Decimal(100),
                SecurityType.ETF,
            )
            for index in range(61)
        ]
        signal = run_strategy("etf_rotation.v1", history, cutoff)
        self.assertEqual(signal.targets, {})

    def test_new_etf_protocols_are_distinct_and_pit_filtered(self) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        history = []
        for asset, step in (("STEADY", Decimal("0.01")), ("NOISY", Decimal("0.02"))):
            price = Decimal("10")
            for index in range(61):
                if asset == "NOISY" and index % 2:
                    price *= Decimal("0.97")
                else:
                    price *= Decimal(1) + step
                history.append(
                    PricePoint(
                        asset,
                        date(2025, 1, 1) + timedelta(days=index),
                        cutoff - timedelta(days=61 - index),
                        price,
                        Decimal("100"),
                        SecurityType.ETF_DOMESTIC_EQUITY,
                    )
                )
        short = run_strategy("etf_momentum_20.v1", history, cutoff, top_n=1)
        risk_adjusted = run_strategy(
            "etf_risk_adjusted_60.v1", history, cutoff, top_n=1
        )
        self.assertEqual(set(short.targets), {"STEADY"})
        self.assertEqual(set(risk_adjusted.targets), {"STEADY"})
        self.assertNotEqual(short.scores["STEADY"], risk_adjusted.scores["STEADY"])


if __name__ == "__main__":
    unittest.main()
