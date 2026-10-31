from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S05SchemaTests(unittest.TestCase):
    def test_stage10_schema_is_append_only_and_checksum_matches(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0004_s05_stage10.sql")
        checksum = hashlib.sha256(sql.read_bytes()).hexdigest()
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version='0004_s05_stage10'"
                ).fetchone()[0],
                checksum,
            )
            self.assertTrue(
                {"stage10_runs", "stage10_differences"}
                <= {
                    x[0]
                    for x in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            )
            row = conn.execute("SELECT result_id FROM stage10_runs LIMIT 1").fetchone()
            if row:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE stage10_runs SET status='PASS' WHERE result_id=?", row
                    )
        finally:
            conn.rollback()
            conn.close()
