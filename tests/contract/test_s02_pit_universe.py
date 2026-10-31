from __future__ import annotations

import unittest
import shutil
import sqlite3
import uuid
from datetime import UTC, date, datetime

from src.quantcore.pit.financials import FinancialRevision, FinancialRevisionStore
from src.quantcore.pit.storage import PITCatalog
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.universe.models import (
    Board,
    Confidence,
    IdentifierVersion,
    RiskStatus,
    SecurityIdentity,
    SecurityStateVersion,
    SecurityType,
    TradingStatus,
)
from src.quantcore.universe.repository import HistoricalSecurityMaster
from src.quantcore.universe.service import (
    DecisionReason,
    HistoricalUniverseService,
    MembershipVersion,
    UniversePolicy,
)


def t(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def identity(asset="asset-cn-1"):
    return SecurityIdentity(asset, SecurityType.STOCK, "SZ", "fixture:master")


def state(
    *,
    state_id="s1",
    asset="asset-cn-1",
    effective_from=date(2024, 1, 1),
    effective_to=None,
    known_from=t(1),
    known_to=None,
    list_date=date(2024, 1, 2),
    delist_date=None,
    name="OLD NAME",
    board=Board.SZSE_MAIN,
    risk=RiskStatus.NORMAL,
    suspended=False,
    confidence=Confidence.VERIFIED,
    revision="r1",
):
    return SecurityStateVersion(
        state_id,
        asset,
        effective_from,
        effective_to,
        known_from,
        known_to,
        list_date,
        delist_date,
        name,
        board,
        risk,
        suspended,
        f"fixture:{revision}",
        confidence,
        revision,
    )


class S02PITUniverseTests(unittest.TestCase):
    def master(self, states):
        identifier = IdentifierVersion(
            "asset-cn-1",
            "TS_CODE",
            "000001.SZ",
            date(1991, 1, 1),
            None,
            t(1),
            None,
            "fixture:id",
            Confidence.VERIFIED,
        )
        return HistoricalSecurityMaster([identity()], [identifier], states)

    def test_future_state_is_not_visible_and_revision_changes_at_known_time(self):
        first = state(known_from=t(1), known_to=t(5), revision="r1", name="FIRST")
        revised = state(state_id="s2", known_from=t(5), revision="r2", name="REVISED")
        master = self.master([first, revised])
        with self.assertRaises(LookupError):
            master.state_as_of(
                "asset-cn-1", date(2024, 2, 1), datetime(2025, 12, 31, tzinfo=UTC)
            )
        self.assertEqual(
            master.state_as_of("asset-cn-1", date(2024, 2, 1), t(4)).name, "FIRST"
        )
        self.assertEqual(
            master.state_as_of("asset-cn-1", date(2024, 2, 1), t(5)).name, "REVISED"
        )

    def test_listing_delisting_and_missing_bar_are_not_guessed(self):
        item = state(delist_date=date(2024, 1, 10))
        self.assertIs(item.trading_status(date(2024, 1, 1)), TradingStatus.NOT_LISTED)
        self.assertIs(item.trading_status(date(2024, 1, 10)), TradingStatus.TRADABLE)
        self.assertIs(item.trading_status(date(2024, 1, 11)), TradingStatus.DELISTED)
        self.assertIs(
            item.trading_status(date(2024, 1, 5), bar_present=False),
            TradingStatus.UNKNOWN,
        )
        self.assertIs(
            state(suspended=None).trading_status(date(2024, 1, 5)),
            TradingStatus.UNKNOWN,
        )

    def test_current_name_does_not_backfill_history(self):
        old = state(effective_to=date(2025, 1, 1), name="OLD")
        new = state(
            state_id="s2",
            effective_from=date(2025, 1, 1),
            known_from=t(2),
            name="CURRENT",
            revision="r2",
        )
        master = self.master([old, new])
        self.assertEqual(
            master.state_as_of("asset-cn-1", date(2024, 6, 1), t(6)).name, "OLD"
        )
        self.assertEqual(
            master.state_as_of("asset-cn-1", date(2025, 6, 1), t(6)).name, "CURRENT"
        )

    def test_unknown_state_fails_closed(self):
        master = self.master(
            [
                state(
                    confidence=Confidence.UNKNOWN,
                    suspended=None,
                    risk=RiskStatus.UNKNOWN,
                )
            ]
        )
        with self.assertRaises(LookupError):
            master.state_as_of("asset-cn-1", date(2024, 2, 1), t(4))
        snapshot = HistoricalUniverseService(master).snapshot(
            date(2024, 2, 1),
            t(4),
            UniversePolicy(),
            held_assets=frozenset({"asset-cn-1"}),
        )
        self.assertEqual(snapshot.selection_members, ())
        self.assertEqual(snapshot.valuation_assets, ("asset-cn-1",))
        self.assertEqual(snapshot.readiness, "BLOCKED")
        self.assertIn(DecisionReason.HELD_FOR_VALUATION, snapshot.decisions[0].reasons)

    def test_holding_is_valued_after_leaving_selection_pool(self):
        master = self.master([state(risk=RiskStatus.ST)])
        snapshot = HistoricalUniverseService(master).snapshot(
            date(2024, 2, 1),
            t(4),
            UniversePolicy(),
            held_assets=frozenset({"asset-cn-1"}),
        )
        self.assertEqual(snapshot.selection_members, ())
        self.assertEqual(snapshot.valuation_assets, ("asset-cn-1",))
        self.assertIn(DecisionReason.RISK_EXCLUDED, snapshot.decisions[0].reasons)

    def test_membership_requires_historical_verified_evidence(self):
        master = self.master([state()])
        future = MembershipVersion(
            "CSI300", "asset-cn-1", date(2024, 1, 1), None, t(8), "fixture:future", True
        )
        snapshot = HistoricalUniverseService(master, [future]).snapshot(
            date(2024, 2, 1), t(4), UniversePolicy(), membership_group="CSI300"
        )
        self.assertEqual(snapshot.selection_members, ())
        self.assertEqual(snapshot.readiness, "BLOCKED")

    def test_financial_revisions_obey_available_at(self):
        r1 = FinancialRevision(
            "asset-cn-1",
            "income",
            date(2025, 12, 31),
            "r1",
            t(2),
            t(2),
            "fixture:r1",
            "a" * 64,
            Confidence.VERIFIED,
            {"profit": 10},
        )
        r2 = FinancialRevision(
            "asset-cn-1",
            "income",
            date(2025, 12, 31),
            "r2",
            t(7),
            t(7),
            "fixture:r2",
            "b" * 64,
            Confidence.VERIFIED,
            {"profit": 12},
        )
        store = FinancialRevisionStore([r1, r2])
        self.assertEqual(store.as_of(t(1)), [])
        self.assertEqual(store.as_of(t(4))[0].values["profit"], 10)
        self.assertEqual(store.as_of(t(8))[0].values["profit"], 12)

    def test_naive_decision_and_available_times_are_rejected(self):
        with self.assertRaises(ValueError):
            FinancialRevision(
                "a",
                "income",
                date(2025, 1, 1),
                "r1",
                datetime(2026, 1, 1),
                t(2),
                "fixture",
                "a" * 64,
                Confidence.VERIFIED,
                {},
            )
        with self.assertRaises(ValueError):
            self.master([state()]).state_as_of(
                "asset-cn-1", date(2024, 2, 1), datetime(2026, 1, 4)
            )

    def test_append_only_catalog_round_trip_and_duplicate_rejection(self):
        cfg = load_v2_config()
        root = cfg.paths.state / "test_tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        database = root / "control.sqlite"
        shutil.copy2(cfg.paths.state / "control.sqlite", database)
        try:
            catalog = PITCatalog(database)
            catalog.append_identity(identity())
            identifier = IdentifierVersion(
                "asset-cn-1",
                "TS_CODE",
                "000001.SZ",
                date(1991, 1, 1),
                None,
                t(1),
                None,
                "fixture:id",
                Confidence.VERIFIED,
            )
            catalog.append_identifier("id-v1", identifier)
            catalog.append_state(state())
            revision = FinancialRevision(
                "asset-cn-1",
                "income",
                date(2025, 12, 31),
                "r1",
                t(2),
                t(2),
                "fixture:r1",
                "a" * 64,
                Confidence.VERIFIED,
                {"profit": 10},
            )
            catalog.append_financial("fin-v1", revision)
            self.assertEqual(
                catalog.load_master().identifier_as_of(
                    "TS_CODE", "000001.SZ", date(2024, 2, 1), t(4)
                ),
                "asset-cn-1",
            )
            self.assertEqual(
                catalog.load_financials().as_of(t(4))[0].values["profit"], 10
            )
            with self.assertRaises(sqlite3.IntegrityError):
                catalog.append_state(state())
        finally:
            shutil.rmtree(root)
