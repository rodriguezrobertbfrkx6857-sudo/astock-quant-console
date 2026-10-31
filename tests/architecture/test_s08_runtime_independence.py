from __future__ import annotations
import unittest
from pathlib import Path


class S08RuntimeArchitectureTests(unittest.TestCase):
    def test_runtime_does_not_import_or_start_web(self):
        text = "\n".join(
            x.read_text("utf-8") for x in Path("src/quantcore/runtime").glob("*.py")
        )
        self.assertNotIn("quantweb", text)
        self.assertNotIn("uvicorn", text)
        self.assertNotIn("FastAPI", text)
