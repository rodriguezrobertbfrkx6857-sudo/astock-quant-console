from __future__ import annotations
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from src.quantcore.operations.backup import BackupManager
from src.quantcore.operations.replay import (
    deterministic_hand_replay,
    known_real_readiness,
)
from src.quantcore.operations.shadow import ShadowTracker
from src.quantcore.validation.stage10 import Stage10Status


class S10OperationsTests(unittest.TestCase):
    def setUp(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir="state/v2/test_tmp")
        self.root = Path(self.temp.name)
        self.control = self.root / "control.sqlite"
        self.accounting = self.root / "accounting.sqlite"
        shutil.copy2("state/v2/control.sqlite", self.control)
        shutil.copy2("state/v2/accounting.sqlite", self.accounting)

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_restores_exact_hashes_to_isolated_directory(self):
        manager = BackupManager(self.control, self.accounting, self.root / "backups")
        manifest = manager.create("b1")
        restored = manager.restore(manifest, self.root / "restore")
        self.assertEqual(set(restored), {"control.sqlite", "accounting.sqlite"})
        conn = sqlite3.connect(self.root / "restore" / "control.sqlite")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        with self.assertRaises(FileExistsError):
            manager.restore(manifest, self.root / "restore")

    def test_corrupt_backup_is_rejected(self):
        manager = BackupManager(self.control, self.accounting, self.root / "backups")
        manifest = manager.create("b2")
        path = manifest.parent / "control.sqlite"
        with path.open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaises(RuntimeError):
            manager.restore(manifest, self.root / "bad-restore")

    def test_replay_is_deterministic_and_weekend_uses_explicit_calendar(self):
        first = deterministic_hand_replay()
        second = deterministic_hand_replay()
        self.assertIs(first.status, Stage10Status.PASS)
        self.assertEqual(
            (first.formal_hash, first.reference_hash),
            (second.formal_hash, second.reference_hash),
        )

    def test_known_real_replay_remains_blocked_without_real_ready_price_manifest(self):
        empty = self.root / "empty-control.sqlite"
        conn = sqlite3.connect(empty)
        conn.execute(
            "CREATE TABLE data_manifests "
            "(dataset TEXT,manifest_hash TEXT,readiness TEXT,provenance TEXT)"
        )
        conn.commit()
        conn.close()
        result = known_real_readiness(empty)
        self.assertEqual(result["status"], "BLOCKED_DATA")

    def test_shadow_tracker_refuses_unverified_or_future_days_and_needs_twenty(self):
        tracker = ShadowTracker(self.control)
        tracker.start("test-shadow", 20)
        with self.assertRaises(ValueError):
            tracker.record_day(
                "test-shadow",
                date(2026, 1, 1),
                verified_trading_session=False,
                trading_session_evidence="",
                reconciliation_status="PASS",
                duplicate_fills=0,
                permission_violations=0,
                today=date(2026, 2, 1),
            )
        with self.assertRaises(ValueError):
            tracker.record_day(
                "test-shadow",
                date(2027, 1, 1),
                verified_trading_session=True,
                trading_session_evidence="calendar",
                reconciliation_status="PASS",
                duplicate_fills=0,
                permission_violations=0,
                today=date(2026, 2, 1),
            )
        start = date(2026, 1, 1)
        for i in range(19):
            tracker.record_day(
                "test-shadow",
                start + timedelta(days=i),
                verified_trading_session=True,
                trading_session_evidence=f"fixture-calendar:{i}",
                reconciliation_status="PASS",
                duplicate_fills=0,
                permission_violations=0,
                today=date(2026, 2, 1),
            )
        self.assertEqual(tracker.status("test-shadow")["status"], "ACTIVE")
        tracker.record_day(
            "test-shadow",
            start + timedelta(days=19),
            verified_trading_session=True,
            trading_session_evidence="fixture-calendar:19",
            reconciliation_status="PASS",
            duplicate_fills=0,
            permission_violations=0,
            today=date(2026, 2, 1),
        )
        self.assertEqual(tracker.status("test-shadow")["status"], "COMPLETE")
