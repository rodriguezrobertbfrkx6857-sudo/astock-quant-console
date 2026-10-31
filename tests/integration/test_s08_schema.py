from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S08SchemaTests(unittest.TestCase):
    def test_both_runtime_migrations_are_versioned(self):
        cfg = load_v2_config()
        pairs = (
            (
                cfg.paths.state / "control.sqlite",
                Path("src/quantcore/data/migrations/0007_s08_runtime.sql"),
                "0007_s08_runtime",
            ),
            (
                cfg.paths.state / "accounting.sqlite",
                Path("src/quantcore/accounting/migrations/0002_s08_outbox.sql"),
                "0002_s08_outbox",
            ),
        )
        for db, sql, version in pairs:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            self.assertEqual(
                conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
                ).fetchone()[0],
                hashlib.sha256(sql.read_bytes()).hexdigest(),
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.close()
