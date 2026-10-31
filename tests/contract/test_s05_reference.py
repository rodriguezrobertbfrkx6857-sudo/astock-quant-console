from __future__ import annotations

import unittest
import shutil
import tempfile
from pathlib import Path
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from reference_engine.engine import RefAction, RefEngine, RefLot, RefMarket
from src.quantcore.accounting.ledger import AccountState, Lot
from src.quantcore.execution.formal import CorporateAction, FormalEngine, MarketState
from src.quantcore.rules.rulebook import default_rulebook
from src.quantcore.universe.models import Board, SecurityType
from src.quantcore.validation.projection import formal_snapshot, reference_snapshot
from src.quantcore.validation.stage10 import Stage10Status, compare_snapshots
from src.quantcore.validation.stage10 import Stage10Store
from src.quantcore.contracts.config import load_v2_config

D = Decimal
ON = date(2024, 1, 2)


def next_session(on: date) -> date:
    return on + timedelta(days=1)


class S05ReferenceTests(unittest.TestCase):
    def paired_buy(self):
        formal_state = AccountState(D("10000"))
        formal = FormalEngine(formal_state, default_rulebook(), next_session)
        reference = RefEngine(D("10000"), next_session)
        formal_orders = formal.plan_targets(
            {},
            {"asset": 100},
            ON,
            {"asset": Board.SSE_MAIN},
            {"asset": SecurityType.STOCK},
        )
        ref_orders = reference.plan_targets(
            {}, {"asset": 100}, ON, {"asset": "SSE_MAIN"}
        )
        formal.execute(
            formal_orders[0],
            ON,
            D("10"),
            market=MarketState(True, D("11"), D("9"), "fixture"),
        )
        reference.execute(
            ref_orders[0], ON, D("10"), RefMarket(True, D("11"), D("9"), "fixture")
        )
        left = formal_snapshot(
            ON, formal_state, formal_orders, formal.fills, {"asset": D("10")}
        )
        right = reference_snapshot(
            ON, reference, ref_orders, reference.fills, {"asset": D("10")}
        )
        return left, right

    def test_independent_buy_matches_every_field(self):
        left, right = self.paired_buy()
        result = compare_snapshots((left,), (right,), input_hash="fixture-hash")
        self.assertIs(result.status, Stage10Status.PASS)
        self.assertTrue(result.evidence_valid)

    def test_cash_dividend_and_t1_match_hand_calculation(self):
        old = Lot("old", "asset", 100, D("10"), date(2023, 1, 1), date(2023, 1, 2))
        ref_old = RefLot(
            "old", "asset", 100, D("10"), date(2023, 1, 1), date(2023, 1, 2)
        )
        formal_state = AccountState(D("0"), [old])
        formal = FormalEngine(formal_state, default_rulebook(), next_session)
        reference = RefEngine(D("0"), next_session, [ref_old])
        formal.apply_action(
            CorporateAction(
                "div", "asset", "CASH_DIVIDEND", ON, ON + timedelta(days=1), D("1")
            ),
            ON,
        )
        reference.apply_action(
            RefAction(
                "div", "asset", "CASH_DIVIDEND", ON, ON + timedelta(days=1), D("1")
            ),
            ON,
        )
        left = formal_snapshot(
            ON,
            formal_state,
            [],
            formal.fills,
            {"asset": D("9")},
            event_receipts=("div:accrual",),
        )
        right = reference_snapshot(
            ON,
            reference,
            [],
            reference.fills,
            {"asset": D("9")},
            event_receipts=("div:accrual",),
        )
        self.assertEqual(left.nav, "1000.00")
        self.assertIs(
            compare_snapshots((left,), (right,), input_hash="hand").status,
            Stage10Status.PASS,
        )

    def test_extra_missing_and_duplicate_fills_fail_bidirectionally(self):
        left, right = self.paired_buy()
        missing = compare_snapshots(
            (left,), (replace(right, fills=()),), input_hash="x"
        )
        extra = compare_snapshots((replace(left, fills=()),), (right,), input_hash="x")
        duplicate = compare_snapshots(
            (replace(left, fills=left.fills + left.fills),), (right,), input_hash="x"
        )
        self.assertTrue(
            all(x.status is Stage10Status.FAIL for x in (missing, extra, duplicate))
        )
        self.assertTrue(
            any(x.category == "duplicate_fill" for x in duplicate.differences)
        )

    def test_cash_fee_position_nav_and_timing_differences_fail_exactly(self):
        left, right = self.paired_buy()
        variants = [
            replace(right, cash="1"),
            replace(right, total_fees="4.99"),
            replace(right, positions=(("asset", 99),)),
            replace(right, nav="9999"),
            replace(right, on="2024-01-03"),
        ]
        for changed in variants:
            with self.subTest(changed=changed):
                self.assertIs(
                    compare_snapshots((left,), (changed,), input_hash="x").status,
                    Stage10Status.FAIL,
                )

    def test_insufficient_and_different_spec_never_create_valid_evidence(self):
        left, right = self.paired_buy()
        insufficient = compare_snapshots(
            (left,), (right,), input_hash="x", sufficient_data=False
        )
        incompatible = compare_snapshots(
            (left,), (right,), input_hash="x", same_specification=False
        )
        self.assertIs(insufficient.status, Stage10Status.INSUFFICIENT_DATA)
        self.assertIs(incompatible.status, Stage10Status.NOT_COMPARABLE)
        self.assertFalse(insufficient.evidence_valid or incompatible.evidence_valid)

    def test_stage10_store_is_idempotent_and_persists_failures(self):
        left, right = self.paired_buy()
        failed = compare_snapshots(
            (left,), (replace(right, cash="0"),), input_hash="store-fixture"
        )
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="state/v2/test_tmp") as temp:
            database = Path(temp) / "control.sqlite"
            shutil.copy2(load_v2_config().paths.state / "control.sqlite", database)
            store = Stage10Store(database)
            first = store.record("s05-fixture-run-isolated", failed)
            second = store.record("s05-fixture-run-isolated", failed)
            self.assertEqual(first, second)

    def test_gold_etf_t0_round_trip_matches_independent_reference(self):
        formal_state = AccountState(D("10000"))
        formal = FormalEngine(formal_state, default_rulebook(), next_session)
        reference = RefEngine(D("10000"), next_session)
        types = {"gold": SecurityType.ETF_GOLD_COMMODITY}
        formal_buy = formal.plan_targets(
            {}, {"gold": 100}, ON, {"gold": Board.SSE_MAIN}, types
        )
        ref_buy = reference.plan_targets(
            {},
            {"gold": 100},
            ON,
            {"gold": "SSE_MAIN"},
            {"gold": "ETF_GOLD_COMMODITY"},
        )
        formal.execute(
            formal_buy[0], ON, D("10"), market=MarketState(True, D("11"), D("9"), "fixture")
        )
        reference.execute(
            ref_buy[0], ON, D("10"), RefMarket(True, D("11"), D("9"), "fixture")
        )
        formal_sell = formal.plan_targets(
            {"gold": 100}, {}, ON, {"gold": Board.SSE_MAIN}, types
        )
        ref_sell = reference.plan_targets(
            {"gold": 100},
            {},
            ON,
            {"gold": "SSE_MAIN"},
            {"gold": "ETF_GOLD_COMMODITY"},
        )
        self.assertIsNotNone(
            formal.execute(
                formal_sell[0],
                ON,
                D("10.1"),
                market=MarketState(True, D("11"), D("9"), "fixture"),
            )
        )
        self.assertIsNotNone(
            reference.execute(
                ref_sell[0], ON, D("10.1"), RefMarket(True, D("11"), D("9"), "fixture")
            )
        )
        left = formal_snapshot(
            ON,
            formal_state,
            formal_buy + formal_sell,
            formal.fills,
            {"gold": D("10.1")},
        )
        right = reference_snapshot(
            ON,
            reference,
            ref_buy + ref_sell,
            reference.fills,
            {"gold": D("10.1")},
        )
        self.assertIs(
            compare_snapshots((left,), (right,), input_hash="etf-t0").status,
            Stage10Status.PASS,
        )


if __name__ == "__main__":
    unittest.main()
