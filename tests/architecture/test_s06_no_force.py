from __future__ import annotations
import unittest
from pathlib import Path


class S06NoForceTests(unittest.TestCase):
    def test_new_research_api_has_no_force_parameter(self):
        text = "\n".join(
            path.read_text("utf-8")
            for path in Path("src/quantcore/research").glob("*.py")
        )
        self.assertNotIn("force_promote", text)
        self.assertNotIn("force_candidate", text)
