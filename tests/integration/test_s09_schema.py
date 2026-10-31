from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S09SchemaTests(unittest.TestCase):
    def test_reporting_schema_is_versioned(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0008_s09_reporting.sql")
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0008_s09_reporting'"
            ).fetchone()[0],
            hashlib.sha256(sql.read_bytes()).hexdigest(),
        )
        self.assertTrue(
            {
                "reporting_publications",
                "canonical_metrics",
                "current_reporting_publication",
            }
            <= {
                x[0]
                for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        conn.close()
