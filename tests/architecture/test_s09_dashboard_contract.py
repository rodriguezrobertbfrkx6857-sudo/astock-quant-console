from __future__ import annotations
import unittest
from pathlib import Path


class S09DashboardContractTests(unittest.TestCase):
    def test_dashboard_uses_only_v2_api_and_does_not_recompute_percentages_or_annualization(
        self,
    ):
        text = "\n".join(
            x.read_text("utf-8") for x in Path("dashboard/src").rglob("*.tsx")
        )
        self.assertNotIn("fetch('/api/", text.replace("fetch('/api/v2/", ""))
        self.assertNotIn("fetch(`/api/", text.replace("fetch(`/api/v2/", ""))
        self.assertNotIn("* 100", text)
        self.assertNotIn("244", text)
        self.assertNotIn("252", text)

    def test_v2_api_has_no_ddl(self):
        text = Path("quantweb/v2_api.py").read_text("utf-8").upper()
        self.assertNotIn("CREATE TABLE", text)
        self.assertNotIn("ALTER TABLE", text)
        self.assertNotIn("DROP TABLE", text)
