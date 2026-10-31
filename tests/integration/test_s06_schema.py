from __future__ import annotations
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S06SchemaTests(unittest.TestCase):
    def test_research_schema_and_initial_queue_are_frozen(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0005_s06_research.sql")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version='0005_s06_research'"
                ).fetchone()[0],
                hashlib.sha256(sql.read_bytes()).hexdigest(),
            )
            rows = conn.execute(
                "SELECT queue_item_id,status FROM research_queue ORDER BY priority"
            ).fetchall()
            self.assertGreaterEqual(len(rows), 10)
            statuses = dict(rows)
            self.assertEqual(statuses["queue-ctrl-v1"], "SUCCEEDED")
            self.assertEqual(
                {status for queue_id, status in rows if queue_id != "queue-ctrl-v1"},
                {"BLOCKED_DATA", "SUCCEEDED"},
            )
            self.assertFalse(any(x[1] == "RUNNING" for x in rows))
            self.assertEqual(rows[0][0], "queue-ctrl-v1")
            self.assertTrue(
                all(statuses[item] == "BLOCKED_DATA" for item in (
                    "queue-r01-v1", "queue-r02-v1", "queue-r03-v1", "queue-r04-v1",
                    "queue-r05-v1", "queue-r06-v1", "queue-r07-v1", "queue-r08-v1", "queue-r09-v1",
                ))
            )
            protocols = [
                json.loads(x[0])
                for x in conn.execute("SELECT protocol_json FROM research_protocols")
            ]
            self.assertTrue(all(x["oos_access"] == "KNOWN_ONLY" for x in protocols))
            self.assertTrue(all(len(x["validation_windows"]) >= 3 for x in protocols))
        finally:
            conn.close()
