from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S10SchemaTests(unittest.TestCase):
    def test_operations_schema_is_versioned(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0009_s10_operations.sql")
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0009_s10_operations'"
            ).fetchone()[0],
            hashlib.sha256(sql.read_bytes()).hexdigest(),
        )
        self.assertTrue(
            {"shadow_acceptance_runs", "shadow_acceptance_days", "recovery_exercises"}
            <= {
                x[0]
                for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        conn.close()
