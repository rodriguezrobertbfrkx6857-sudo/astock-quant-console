from __future__ import annotations
import hashlib
import sqlite3
import unittest
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.contracts.safety import PROJECT_ROOT


class S03SchemaTests(unittest.TestCase):
    def test_oos_schema_migration_and_known_lower_bound(self):
        db = load_v2_config().paths.state / "control.sqlite"
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {"exposure_events", "window_reservations", "access_receipts"}.issubset(
                    tables
                )
            )
            rows = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0003_s03_oos'"
            ).fetchall()
            count = conn.execute("SELECT COUNT(*) FROM exposure_events").fetchone()[0]
            future = conn.execute(
                "SELECT COUNT(*) FROM exposure_events WHERE end_date>='2099-01-01'"
            ).fetchone()[0]
            synthetic = conn.execute(
                "SELECT COUNT(*) FROM exposure_events WHERE provenance='SYNTHETIC'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(count, 8)
        self.assertEqual(future, 0)
        self.assertEqual(synthetic, 1)
        sql = (
            PROJECT_ROOT
            / "src"
            / "quantcore"
            / "data"
            / "migrations"
            / "0003_s03_oos.sql"
        )
        self.assertEqual(rows[0][0], hashlib.sha256(sql.read_bytes()).hexdigest())
