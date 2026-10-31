from __future__ import annotations

import ast
import unittest
from pathlib import Path


class S05ReferenceIndependenceTests(unittest.TestCase):
    def test_reference_package_does_not_import_formal_accounting_or_rulebook(self):
        forbidden = (
            "quantcore.execution",
            "quantcore.accounting",
            "quantcore.rules",
            "qforge",
        )
        for path in Path("reference_engine").rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            self.assertFalse(
                any(name.startswith(forbidden) for name in imports), (path, imports)
            )


if __name__ == "__main__":
    unittest.main()
