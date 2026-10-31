from __future__ import annotations
import hashlib
import sqlite3
import unittest
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config


class S11SchemaQueueTests(unittest.TestCase):
    def test_event_schema_and_all_unready_queue_items_are_blocked(self):
        db = load_v2_config().paths.state / "control.sqlite"
        sql = Path("src/quantcore/data/migrations/0010_s11_events_queue.sql")
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0010_s11_events_queue'"
            ).fetchone()[0],
            hashlib.sha256(sql.read_bytes()).hexdigest(),
        )
        self.assertTrue(
            {
                "event_documents",
                "event_revisions",
                "event_security_links",
                "event_study_results",
                "queue_readiness_assessments",
            }
            <= {
                x[0]
                for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        rows = conn.execute(
            "SELECT status,count(*) FROM research_queue GROUP BY status"
        ).fetchall()
        statuses = dict(rows)
        self.assertEqual(statuses.get("BLOCKED_DATA"), 9)
        self.assertGreaterEqual(statuses.get("SUCCEEDED", 0), 1)
        self.assertGreaterEqual(
            conn.execute(
                "SELECT count(DISTINCT queue_item_id) "
                "FROM queue_readiness_assessments"
            ).fetchone()[0],
            10,
        )
        conn.close()
