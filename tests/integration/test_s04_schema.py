from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path

from src.quantcore.contracts.config import load_v2_config


class S04SchemaTests(unittest.TestCase):
    def test_accounting_schema_is_versioned_and_has_integrity_constraints(self):
        config = load_v2_config()
        database = config.paths.state / "accounting.sqlite"
        migration = Path("src/quantcore/accounting/migrations/0001_s04_accounting.sql")
        checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
        conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0001_s04_accounting'"
            ).fetchone()
            self.assertEqual(row[0], checksum)
            tables = {
                x[0]
                for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "accounts",
                    "lots",
                    "receivables",
                    "orders",
                    "fills",
                    "journal_entries",
                    "journal_lines",
                    "corporate_action_receipts",
                    "daily_nav",
                }
                <= tables
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden(value TEXT)")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
