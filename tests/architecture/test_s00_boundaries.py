from __future__ import annotations

import unittest

from src.quantcore.contracts.safety import PROJECT_ROOT


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_quantweb_does_not_use_write_capable_connectors(self) -> None:
        source = (PROJECT_ROOT / "quantweb" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("db.connect_market()", source)
        self.assertNotIn("db.connect_lab()", source)
        self.assertIn("LocalSQLiteRepository", source)

    def test_default_writable_data_path_is_not_legacy(self) -> None:
        source = (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
        self.assertIn('"data_dir": "data/v2"', source)
        first_line = next(line for line in source.splitlines() if '"data_dir"' in line)
        self.assertNotIn("量化3", first_line)

    def test_read_repository_has_no_schema_hook(self) -> None:
        source = (PROJECT_ROOT / "quantweb" / "read_repository.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("executescript", source)
        self.assertNotIn("CREATE TABLE", source)
        self.assertIn("mode=ro&immutable=1", source)

    def test_legacy_data_is_not_hardcoded_as_real(self) -> None:
        source = (PROJECT_ROOT / "quantweb" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('"data_source": "REAL"', source)
        self.assertNotIn('"overall_source": "REAL"', source)
        self.assertIn('LEGACY_SOURCE_STATUS = "LOCAL_ARCHIVE"', source)
