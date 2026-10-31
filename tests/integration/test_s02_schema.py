from __future__ import annotations

import hashlib
import sqlite3
import unittest
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.contracts.safety import PROJECT_ROOT


class S02SchemaTests(unittest.TestCase):
    def test_s02_schema_and_checksum(self):
        target = load_v2_config().paths.state / "control.sqlite"
        conn = sqlite3.connect(
            f"file:{target.as_posix()}?mode=ro&immutable=1", uri=True
        )
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            expected = {
                "security_identities",
                "security_identifier_versions",
                "security_state_versions",
                "universe_membership_versions",
                "financial_revisions",
            }
            self.assertTrue(expected.issubset(tables))
            rows = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0002_s02_pit_universe'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        path = (
            PROJECT_ROOT
            / "src"
            / "quantcore"
            / "data"
            / "migrations"
            / "0002_s02_pit_universe.sql"
        )
        self.assertEqual(rows[0][0], hashlib.sha256(path.read_bytes()).hexdigest())
