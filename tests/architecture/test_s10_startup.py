from __future__ import annotations
import unittest
from pathlib import Path


class S10StartupTests(unittest.TestCase):
    def test_local_startup_uses_v2_config_and_no_broker(self):
        text = Path("scripts/v2/start_local.py").read_text("utf-8")
        self.assertIn("validate_runtime_safety", text)
        self.assertNotIn("broker", text.lower())
        self.assertNotIn("0.0.0.0", text)

    def test_runbooks_and_launcher_exist(self):
        for path in (
            "start_v2.bat",
            "docs/integrated_v2/OPERATIONS.md",
            "docs/integrated_v2/RECOVERY.md",
            "docs/integrated_v2/SUPPORTED_SCOPE.md",
        ):
            self.assertTrue(Path(path).is_file(), path)
