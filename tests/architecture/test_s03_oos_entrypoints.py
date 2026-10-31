from __future__ import annotations
import unittest
from src.quantcore.contracts.safety import PROJECT_ROOT, SafetyViolation


class S03EntrypointTests(unittest.TestCase):
    def test_quantweb_result_routes_call_unified_guard(self):
        source = (PROJECT_ROOT / "quantweb" / "app.py").read_text("utf-8")
        self.assertIn("OOSAccessService", source)
        self.assertGreaterEqual(source.count("_guard_result_range("), 8)
        self.assertIn("AccessPurpose.CHART", source)
        self.assertIn("AccessPurpose.EVENT", source)

    def test_legacy_evaluator_is_fail_closed(self):
        from qforge.research import evaluator

        with self.assertRaises(SafetyViolation):
            evaluator.run_backtest("renamed", {}, "20200101", "20200102")

    def test_legacy_cli_paths_are_explicitly_disabled(self):
        source = (PROJECT_ROOT / "qforge" / "cli.py").read_text("utf-8")
        for phrase in (
            "legacy research is disabled",
            "legacy evaluation is disabled",
            "legacy backtest entry is disabled",
            "legacy OOS registry is disabled",
            "legacy event study is disabled",
        ):
            self.assertIn(phrase, source)
