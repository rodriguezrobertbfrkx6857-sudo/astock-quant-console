from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S07SchemaTests(unittest.TestCase):
    def test_qualification_schema_is_versioned_and_append_only(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0006_s07_paper_qualification.sql")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version='0006_s07_paper_qualification'"
                ).fetchone()[0],
                hashlib.sha256(sql.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                {"paper_qualifications", "qualification_events"}
                <= {
                    x[0]
                    for x in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            )
        finally:
            conn.close()
