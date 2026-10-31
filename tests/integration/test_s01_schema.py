from __future__ import annotations

import hashlib
import sqlite3
import unittest

from src.quantcore.contracts.config import load_v2_config
from src.quantcore.contracts.safety import PROJECT_ROOT


class S01SchemaTests(unittest.TestCase):
    def test_control_schema_is_versioned_and_complete(self) -> None:
        cfg = load_v2_config()
        target = cfg.paths.state / "control.sqlite"
        self.assertTrue(target.is_file())
        conn = sqlite3.connect(
            f"file:{target.as_posix()}?mode=ro&immutable=1", uri=True
        )
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "schema_migrations",
                    "raw_attempts",
                    "data_manifests",
                    "data_publications",
                    "quality_reports",
                }.issubset(tables)
            )
            rows = conn.execute(
                "SELECT version, checksum FROM schema_migrations "
                "WHERE version='0001_s01_data_control'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        migration = (
            PROJECT_ROOT
            / "src"
            / "quantcore"
            / "data"
            / "migrations"
            / "0001_s01_data_control.sql"
        )
        self.assertEqual(rows[0][1], hashlib.sha256(migration.read_bytes()).hexdigest())
