from __future__ import annotations
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from src.quantcore.accounting.ledger import AccountState
from src.quantcore.accounting.storage import AccountingStore, OutboxMessage
from src.quantcore.runtime.jobs import FencingViolation, JobSpec, RuntimeStore
from src.quantcore.runtime.outbox import OutboxRelay


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value


class S08RuntimeTests(unittest.TestCase):
    def setUp(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir="state/v2/test_tmp")
        self.control = Path(self.temp.name) / "control.sqlite"
        self.accounting = Path(self.temp.name) / "accounting.sqlite"
        shutil.copy2("state/v2/control.sqlite", self.control)
        shutil.copy2("state/v2/accounting.sqlite", self.accounting)
        self.clock = Clock()
        self.store = RuntimeStore(self.control, self.clock)
        conn = sqlite3.connect(self.control)
        conn.execute("DELETE FROM runtime_attempts")
        conn.execute("DELETE FROM runtime_dependencies")
        conn.execute("DELETE FROM runtime_jobs")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, name, max_attempts=3):
        return JobSpec(
            name,
            date(2026, 1, 1),
            "snapshot",
            "engine-v1",
            {"x": 1},
            "DAILY",
            max_attempts,
        )

    def test_job_key_is_idempotent_and_dag_blocks_until_parent_success(self):
        parent = self.store.register(self.spec("parent"))
        child = self.store.register(self.spec("child"), (parent,))
        self.assertEqual(parent, self.store.register(self.spec("parent")))
        lease = self.store.acquire("worker", loop_kind="DAILY")
        self.assertEqual(lease.job_key, parent)
        self.store.finish(lease, success=True)
        self.assertEqual(self.store.acquire("worker", loop_kind="DAILY").job_key, child)

    def test_expired_lease_increments_fence_and_old_worker_is_rejected(self):
        key = self.store.register(self.spec("fence"))
        old = self.store.acquire("old", loop_kind="DAILY", lease_seconds=10)
        self.clock.value += timedelta(seconds=11)
        new = self.store.acquire("new", loop_kind="DAILY", lease_seconds=10)
        self.assertEqual(new.job_key, key)
        self.assertGreater(new.fencing_token, old.fencing_token)
        with self.assertRaises(FencingViolation):
            self.store.checkpoint(old, {"bad": True})
        self.store.heartbeat(new)
        self.store.checkpoint(new, {"offset": 1})
        self.assertEqual(self.store.finish(new, success=True), "SUCCEEDED")

    def test_expired_owner_cannot_renew_before_takeover(self):
        self.store.register(self.spec("expired-heartbeat"))
        lease = self.store.acquire("slow", loop_kind="DAILY", lease_seconds=10)
        self.clock.value += timedelta(seconds=11)
        with self.assertRaises(FencingViolation):
            self.store.heartbeat(lease)

    def test_retries_are_finite_and_attempts_are_retained(self):
        self.store.register(self.spec("limited", 2))
        one = self.store.acquire("w", loop_kind="DAILY")
        self.assertEqual(
            self.store.finish(one, success=False, error_code="E1"), "WAITING_RETRY"
        )
        two = self.store.acquire("w", loop_kind="DAILY")
        self.assertEqual(
            self.store.finish(two, success=False, error_code="E2"), "FAILED"
        )
        self.assertIsNone(self.store.acquire("w", loop_kind="DAILY"))
        conn = sqlite3.connect(self.control)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM runtime_attempts").fetchone()[0], 2
        )
        conn.close()

    def test_outbox_recovers_after_control_publish_without_reexecuting_business(self):
        accounts = AccountingStore(self.accounting)
        state = AccountState(Decimal("100"))
        message = OutboxMessage(
            "message-1",
            "business-1",
            "PAPER_COMMITTED",
            {"account": "a", "cash": "100"},
        )
        accounts.commit("a", state, outbox=(message,))
        relay = OutboxRelay(self.accounting, self.control)
        with self.assertRaises(RuntimeError):
            relay.relay_once(fail_after_publish=True)
        # The copied accounting fixture may already contain real PAPER
        # outbox messages; recovery must drain all pending messages while
        # preserving the exactly-once assertion for message-1 below.
        self.assertGreaterEqual(relay.relay_once(), 1)
        self.assertEqual(relay.relay_once(), 0)
        conn = sqlite3.connect(self.control)
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM runtime_events WHERE message_id='message-1'"
            ).fetchone()[0],
            1,
        )
        conn.close()
        conn = sqlite3.connect(self.accounting)
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM business_events WHERE business_event_id='business-1'"
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            conn.execute(
                "SELECT delivered_at FROM accounting_outbox WHERE message_id='message-1'"
            ).fetchone()[0]
        )
        conn.close()
