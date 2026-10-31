from __future__ import annotations

import unittest

from quantweb import v2_api
from src.quantcore.compat.local_archive import LEGACY_PROVENANCE, LocalArchiveAdapter
from src.quantcore.contracts.config import load_v2_config


class ProductParityContractTests(unittest.TestCase):
    def test_legacy_adapter_is_query_only_and_exposes_inventory(self) -> None:
        cfg = load_v2_config()
        adapter = LocalArchiveAdapter(cfg.paths.legacy_archive)
        check = adapter.readonly_check()
        self.assertEqual(check["status"], "PASS")
        self.assertTrue(all(item["query_only"] == 1 for item in check["checks"]))
        self.assertFalse(check["legacy_write_attempted"])
        self.assertGreaterEqual(len(adapter.factors(200)), 15)
        self.assertGreaterEqual(len(adapter.strategies(200)), 7)
        self.assertGreaterEqual(len(adapter.backtests(limit=200)), 14)
        self.assertGreater(adapter.events(3)["count"], 0)

    def test_v2_product_endpoints_keep_legacy_evidence_separate(self) -> None:
        factors = v2_api.factors()
        strategies = v2_api.strategies()
        events = v2_api.events()
        self.assertEqual(factors["provenance"], "REAL")
        self.assertGreaterEqual(factors["v2_count"], 2)
        self.assertIn(strategies["state"], {"V2_AND_LEGACY_EVIDENCE", "V2_REGISTRY_AND_LEGACY_EVIDENCE"})
        self.assertTrue(any(item.get("provenance") == LEGACY_PROVENANCE for item in strategies["strategies"]))
        self.assertGreater(events["count"], 0)
        self.assertEqual(events["live_vendor"], "PENDING_VENDOR")

    def test_v2_replay_detail_contains_serialized_curve(self) -> None:
        listing = v2_api.product_backtests(limit=200)
        v2_item = next(item for item in listing["backtests"] if item.get("v2_revalidated"))
        detail = v2_api.product_backtest_detail(v2_item["backtest_id"])
        nav = v2_api.product_backtest_nav(v2_item["backtest_id"])
        self.assertIn(detail["state"], {"V2_REVALIDATED_REPLAY_DETAIL", "V2_STRATEGY_RUN_DETAIL"})
        self.assertIn(nav["state"], {"V2_REPLAY_NAV", "V2_STRATEGY_RUN_NAV"})
        self.assertGreaterEqual(len(nav["series"]), 3)


if __name__ == "__main__":
    unittest.main()
