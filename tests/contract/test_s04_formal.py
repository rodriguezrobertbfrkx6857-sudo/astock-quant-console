from __future__ import annotations
import unittest
import sqlite3
import tempfile
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal
from src.quantcore.accounting.ledger import AccountState, Lot
from src.quantcore.accounting.storage import AccountingStore, PersistenceConflict
from src.quantcore.execution.formal import (
    CorporateAction,
    FormalEngine,
    MarketState,
    Order,
    OrderStatus,
)
from src.quantcore.rules.rulebook import UnsupportedRuleError, default_rulebook
from src.quantcore.universe.models import Board, SecurityType

D = Decimal


def next_session(on):
    return on + timedelta(days=1)


def order(oid, side, shares, on=date(2024, 1, 2), board=Board.SSE_MAIN):
    return Order(oid, "asset", side, shares, on, board, SecurityType.STOCK)


NORMAL = MarketState(True, D("99"), D("1"), "fixture:limits")


class S04FormalTests(unittest.TestCase):
    def test_rule_boundaries_quantity_and_tax(self):
        book = default_rulebook()
        pre = book.resolve(date(2023, 8, 27), Board.SSE_MAIN, SecurityType.STOCK)
        post = book.resolve(date(2023, 8, 28), Board.SSE_MAIN, SecurityType.STOCK)
        self.assertEqual(pre.fees("SELL", D("10000")), D("15.00"))
        self.assertEqual(post.fees("SELL", D("10000")), D("10.00"))
        with self.assertRaises(ValueError):
            post.validate_buy(200 + 50)
        star = book.resolve(date(2024, 1, 1), Board.STAR, SecurityType.STOCK)
        star.validate_buy(201)
        with self.assertRaises(ValueError):
            star.validate_buy(199)
        with self.assertRaises(UnsupportedRuleError):
            book.resolve(date(2021, 11, 14), Board.BSE, SecurityType.STOCK)
        bse = book.resolve(date(2024, 1, 1), Board.BSE, SecurityType.STOCK)
        self.assertEqual(bse.settlement_sessions, 1)
        domestic = book.resolve(
            date(2024, 1, 1), Board.SSE_MAIN, SecurityType.ETF_DOMESTIC_EQUITY
        )
        bond = book.resolve(date(2024, 1, 1), Board.SSE_MAIN, SecurityType.ETF_BOND)
        self.assertEqual(
            (domestic.settlement_sessions, bond.settlement_sessions), (1, 0)
        )
        self.assertEqual(bond.stamp_tax_sell, D("0"))
        with self.assertRaises(UnsupportedRuleError):
            book.resolve(date(2024, 1, 1), Board.SSE_MAIN, SecurityType.ETF)
        self.assertTrue(all(x.sources for x in book.versions))

    def test_cash_dividend_never_increases_shares_and_nav_is_hand_calculated(self):
        state = AccountState(
            D("0"),
            [Lot("old", "asset", 100, D("10"), date(2023, 1, 1), date(2023, 1, 2))],
        )
        engine = FormalEngine(state, default_rulebook(), next_session)
        action = CorporateAction(
            "div1", "asset", "CASH_DIVIDEND", date(2024, 1, 2), date(2024, 1, 3), D("1")
        )
        engine.apply_action(action, date(2024, 1, 2))
        engine.apply_action(action, date(2024, 1, 2))
        self.assertEqual(state.shares("asset"), 100)
        self.assertEqual(state.receivables["div1"], D("100.00"))
        self.assertEqual(state.nav({"asset": D("9")}), D("1000.00"))
        engine.apply_action(action, date(2024, 1, 3))
        engine.apply_action(action, date(2024, 1, 3))
        self.assertEqual(state.cash, D("100.00"))
        self.assertEqual(state.shares("asset"), 100)
        self.assertEqual(len(state.journal), 2)

    def test_t1_locks_only_new_lot_and_partial_fill_continues(self):
        state = AccountState(
            D("10000"),
            [Lot("old", "asset", 100, D("10"), date(2024, 1, 1), date(2024, 1, 2))],
        )
        engine = FormalEngine(state, default_rulebook(), next_session)
        buy = order("buy", "BUY", 100)
        self.assertIsNotNone(
            engine.execute(buy, date(2024, 1, 2), D("10"), market=NORMAL)
        )
        sell = order("sell", "SELL", 150)
        first = engine.execute(sell, date(2024, 1, 2), D("10"), market=NORMAL)
        self.assertEqual(first.shares, 100)
        self.assertIs(sell.status, OrderStatus.PARTIAL)
        self.assertEqual(state.available_shares("asset", date(2024, 1, 2)), 0)
        second = engine.execute(sell, date(2024, 1, 3), D("10"), market=NORMAL)
        self.assertEqual(second.shares, 50)
        self.assertIs(sell.status, OrderStatus.FILLED)
        self.assertEqual(state.shares("asset"), 50)

    def test_zero_or_missing_target_produces_full_exit(self):
        planned = FormalEngine.plan_targets(
            {"asset": 100},
            {"asset": 0},
            date(2024, 1, 2),
            {"asset": Board.SSE_MAIN},
            {"asset": SecurityType.STOCK},
        )
        self.assertEqual((planned[0].side, planned[0].shares), ("SELL", 100))
        missing = FormalEngine.plan_targets(
            {"asset": 100},
            {},
            date(2024, 1, 2),
            {"asset": Board.SSE_MAIN},
            {"asset": SecurityType.STOCK},
        )
        self.assertEqual((missing[0].side, missing[0].shares), ("SELL", 100))

    def test_illegal_sell_does_not_credit_cash(self):
        state = AccountState(D("50"))
        engine = FormalEngine(state, default_rulebook(), next_session)
        sell = order("bad", "SELL", 100)
        self.assertIsNone(
            engine.execute(sell, date(2024, 1, 2), D("10"), market=NORMAL)
        )
        self.assertEqual(state.cash, D("50"))
        self.assertEqual(state.journal, [])

    def test_order_receipt_is_idempotent_and_entries_balance(self):
        state = AccountState(D("10000"))
        engine = FormalEngine(state, default_rulebook(), next_session)
        buy = order("same", "BUY", 200)
        first = engine.execute(
            buy, date(2024, 1, 2), D("10"), market=NORMAL, max_fill=100
        )
        second = engine.execute(
            buy, date(2024, 1, 2), D("10"), market=NORMAL, max_fill=100
        )
        self.assertIs(first, second)
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(state.shares("asset"), 100)
        for entry in state.journal:
            self.assertEqual(
                sum((x.debit for x in entry.lines), D()),
                sum((x.credit for x in entry.lines), D()),
            )

    def test_unknown_market_and_limit_locked_do_not_fill(self):
        state = AccountState(D("10000"))
        engine = FormalEngine(state, default_rulebook(), next_session)
        self.assertIsNone(
            engine.execute(
                order("unknown", "BUY", 100),
                date(2024, 1, 2),
                D("10"),
                market=MarketState(None, None, None, ""),
            )
        )
        locked = order("locked", "BUY", 100)
        self.assertIsNone(
            engine.execute(
                locked,
                date(2024, 1, 2),
                D("11"),
                market=MarketState(True, D("11"), D("9"), "fixture"),
            )
        )
        self.assertEqual(len(engine.fills), 0)


class S04PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path("state/v2/test_tmp"))
        self.db = Path(self.temp.name) / "accounting.sqlite"
        sql = Path(
            "src/quantcore/accounting/migrations/0001_s04_accounting.sql"
        ).read_text("utf-8")
        conn = sqlite3.connect(self.db)
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?)",
            ("0001_s04_accounting", "fixture", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_transactional_round_trip_and_repeat_commit(self):
        state = AccountState(D("10000"))
        engine = FormalEngine(state, default_rulebook(), next_session)
        buy = order("persist-buy", "BUY", 100)
        fill = engine.execute(buy, date(2024, 1, 2), D("10"), market=NORMAL)
        store = AccountingStore(self.db)
        store.commit("paper", state, orders=(buy,), fills=(fill,))
        store.commit("paper", state, orders=(buy,), fills=(fill,))
        restored = store.load("paper")
        self.assertEqual(restored.cash, state.cash)
        self.assertEqual(restored.shares("asset"), 100)
        self.assertEqual(len(restored.journal), 1)
        self.assertEqual(store.load_orders("paper")[0].status, OrderStatus.FILLED)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 1
        )
        conn.close()

    def test_immutable_fill_conflict_rolls_back_everything(self):
        state = AccountState(D("10000"))
        engine = FormalEngine(state, default_rulebook(), next_session)
        buy = order("conflict-buy", "BUY", 100)
        fill = engine.execute(buy, date(2024, 1, 2), D("10"), market=NORMAL)
        store = AccountingStore(self.db)
        store.commit("paper", state, orders=(buy,), fills=(fill,))
        altered = type(fill)(
            fill.fill_id,
            fill.order_id,
            fill.asset_id,
            fill.side,
            fill.on,
            fill.shares,
            D("11"),
            fill.gross,
            fill.fee,
            fill.rule_id,
        )
        state.cash = D("1")
        with self.assertRaises(PersistenceConflict):
            store.commit("paper", state, orders=(buy,), fills=(altered,))
        self.assertNotEqual(store.load("paper").cash, D("1"))
