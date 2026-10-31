from __future__ import annotations
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from src.quantcore.events.store import EventDocument, EventRevision, EventStore


def H(x):
    return hashlib.sha256(x.encode()).hexdigest()


class S11EventTests(unittest.TestCase):
    def setUp(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir="state/v2/test_tmp")
        self.db = Path(self.temp.name) / "control.sqlite"
        shutil.copy2("state/v2/control.sqlite", self.db)
        self.store = EventStore(self.db)
        pub = datetime(2020, 1, 2, 1, tzinfo=UTC)
        first = pub + timedelta(minutes=5)
        self.document = EventDocument(
            "event-1",
            "https://example.invalid/source",
            "fixture-source",
            pub,
            first,
            H("raw"),
            "REAL",
        )
        self.store.add(
            self.document,
            EventRevision("event-1", "r1", first, H("text1"), "original", "BUYBACK"),
        )
        self.store.add(
            self.document,
            EventRevision(
                "event-1",
                "r2",
                first + timedelta(days=1),
                H("text2"),
                "revised",
                "BUYBACK",
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_event_revision_is_only_visible_after_available_at(self):
        before = self.store.as_of("event-1", self.document.first_seen_at)
        after = self.store.as_of(
            "event-1", self.document.first_seen_at + timedelta(days=2)
        )
        self.assertEqual(before["revision_id"], "r1")
        self.assertEqual(after["revision_id"], "r2")
        self.assertIsNone(self.store.as_of("event-1", self.document.published_at))

    def test_invalid_event_time_and_hash_are_rejected(self):
        with self.assertRaises(ValueError):
            EventDocument(
                "x",
                "url",
                "s",
                datetime(2020, 1, 2),
                datetime(2020, 1, 3),
                "bad",
                "REAL",
            )
        with self.assertRaises(ValueError):
            EventDocument(
                "x",
                "url",
                "s",
                self.document.first_seen_at,
                self.document.published_at,
                H("x"),
                "REAL",
            )

    def test_event_window_uses_unified_oos_authority(self):
        receipt = self.store.authorize_study(
            H("protocol"), H("snapshot"), date(2020, 1, 2), date(2020, 1, 3)
        )
        self.assertEqual(receipt.decision.value, "ALLOW_KNOWN")
        with self.assertRaises(PermissionError):
            self.store.authorize_study(
                H("protocol"), H("snapshot"), date(2017, 1, 1), date(2017, 1, 2)
            )

    def test_pass_study_requires_raw_and_excess_return_real_source_and_benchmark(self):
        receipt = self.store.authorize_study(
            H("protocol"), H("snapshot"), date(2020, 1, 2), date(2020, 1, 3)
        )
        with self.assertRaises(ValueError):
            self.store.record_study(
                "bad",
                "event-1",
                H("protocol"),
                H("snapshot"),
                date(2020, 1, 2),
                date(2020, 1, 3),
                receipt.receipt_id,
                raw_return="0.1",
                excess_car=None,
                benchmark_id=None,
                status="PASS",
            )
        self.store.record_study(
            "ok",
            "event-1",
            H("protocol"),
            H("snapshot"),
            date(2020, 1, 2),
            date(2020, 1, 3),
            receipt.receipt_id,
            raw_return="0.10",
            excess_car="0.03",
            benchmark_id="benchmark",
            status="PASS",
        )
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute(
                "SELECT raw_return,excess_car FROM event_study_results WHERE study_id='ok'"
            ).fetchone(),
            ("0.10", "0.03"),
        )
        conn.close()
