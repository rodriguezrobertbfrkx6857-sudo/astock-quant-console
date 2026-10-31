"""Tests for the research-queue state machine.

Proves correctness of:
- Valid transitions through the full lifecycle (PLANNED → READY → RUNNING → SUCCEEDED).
- Invalid transitions are rejected (e.g. PLANNED → RUNNING, SUCCEEDED → anything).
- BLOCKED_DATA items cannot skip directly to RUNNING; they must pass through READY.
- Idempotent registration (INSERT OR IGNORE).
- Stage09 recording requires a SUCCEEDED queue item.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from src.quantcore.research.queue import ResearchStore
from src.quantcore.research.registry import (
    QueueStatus,
    ResearchProtocol,
    Stage09Rating,
    Stage09Result,
    StrategyVersion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE strategy_families (
    family_id TEXT PRIMARY KEY, control_only INTEGER DEFAULT 0
);
CREATE TABLE research_protocols (
    protocol_hash TEXT PRIMARY KEY, protocol_id TEXT,
    family_id TEXT, protocol_json TEXT, created_at TEXT
);
CREATE TABLE strategy_versions (
    strategy_version_id TEXT PRIMARY KEY, family_id TEXT,
    protocol_hash TEXT, source_code_hash TEXT,
    snapshot_hash TEXT, universe_version TEXT, created_at TEXT
);
CREATE TABLE research_queue (
    queue_item_id TEXT PRIMARY KEY, strategy_version_id TEXT,
    protocol_hash TEXT, priority INTEGER, status TEXT,
    attempt INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE stage09_results (
    result_id TEXT PRIMARY KEY, queue_item_id TEXT,
    protocol_hash TEXT, snapshot_hash TEXT, rating TEXT,
    reasons TEXT, primary_metric TEXT,
    effective_trades INTEGER, validation_blocks INTEGER,
    created_at TEXT
);
"""

_A_HASH = "a" * 64
_B_HASH = "b" * 64
_C_HASH = "c" * 64


@pytest.fixture()
def store(tmp_path: Path) -> ResearchStore:
    """Return a ``ResearchStore`` backed by a freshly-initialised database."""
    db = tmp_path / "queue.sqlite"
    sqlite3.connect(db).executescript(_SCHEMA)

    with patch("src.quantcore.research.queue.PathBoundary") as MockBoundary:
        MockBoundary.return_value.require_writable.side_effect = lambda p: Path(p)
        return ResearchStore(db)


def _protocol(**overrides: object) -> ResearchProtocol:
    """Build a minimal valid ``ResearchProtocol`` for testing."""
    defaults = dict(
        protocol_id="R01.TEST",
        family_id="ETF_ROTATION",
        hypothesis="test hypothesis",
        falsifier="test falsifier",
        source_code_hash=_A_HASH,
        parameter_grid=(("top_n", (1,)),),
        direction="POSITIVE",
        universe_version="v1",
        data_dependencies=("canonical_prices",),
        availability_policy="KNOWN_ONLY",
        rebalance_and_execution="MONTHLY",
        holding_constraints="NONE",
        cost_model="FIXED",
        train_window=("2020-01-01", "2022-12-31"),
        validation_windows=(
            ("2023-01-01", "2023-06-30"),
            ("2023-07-01", "2023-12-31"),
            ("2024-01-01", "2024-12-31"),
        ),
        label_horizon=0,
        purge_sessions=0,
        embargo_sessions=0,
        controls=("none",),
        primary_metric="total_return",
        statistics_policy="FROZEN",
        family_budget=1,
        stop_rule="NONE",
        oos_access="KNOWN_ONLY",
        publication_level="FULL",
        owner_version="v1",
        candidate_threshold=Decimal("0.01"),
    )
    defaults.update(overrides)
    return ResearchProtocol(**defaults)  # type: ignore[arg-type]


def _version(protocol: ResearchProtocol) -> StrategyVersion:
    """Build a ``StrategyVersion`` whose hashes bind to *protocol*."""
    return StrategyVersion(
        strategy_version_id="sv-1",
        family_id=protocol.family_id,
        protocol_hash=protocol.protocol_hash,
        source_code_hash=protocol.source_code_hash,
        snapshot_hash=_B_HASH,
        universe_version=protocol.universe_version,
    )


def _status(store: ResearchStore, item_id: str) -> str:
    """Look up the current status of a queue item."""
    db_path = str(store.database)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM research_queue WHERE queue_item_id=?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0]


# ---------------------------------------------------------------------------
# State-machine transitions
# ---------------------------------------------------------------------------


class TestValidTransitions:
    def test_full_lifecycle(self, store: ResearchStore) -> None:
        """PLANNED → READY → RUNNING → SUCCEEDED is the happy path."""
        proto = _protocol()
        store.register(proto, _version(proto), "Q-001", priority=1)

        store.transition("Q-001", QueueStatus.READY)
        assert _status(store, "Q-001") == QueueStatus.READY.value

        store.transition("Q-001", QueueStatus.RUNNING)
        assert _status(store, "Q-001") == QueueStatus.RUNNING.value

        store.transition("Q-001", QueueStatus.SUCCEEDED)
        assert _status(store, "Q-001") == QueueStatus.SUCCEEDED.value

    def test_blocked_data_to_ready(self, store: ResearchStore) -> None:
        """BLOCKED_DATA can move to READY once data arrives."""
        proto = _protocol(protocol_id="R02.TEST")
        store.register(proto, _version(proto), "Q-002", priority=1)

        store.transition("Q-002", QueueStatus.BLOCKED_DATA)
        assert _status(store, "Q-002") == QueueStatus.BLOCKED_DATA.value

        store.transition("Q-002", QueueStatus.READY)
        assert _status(store, "Q-002") == QueueStatus.READY.value

    def test_failed_can_retry(self, store: ResearchStore) -> None:
        """FAILED items may return to READY for a retry."""
        proto = _protocol(protocol_id="R03.TEST")
        store.register(proto, _version(proto), "Q-003", priority=1)

        store.transition("Q-003", QueueStatus.READY)
        store.transition("Q-003", QueueStatus.RUNNING)
        store.transition("Q-003", QueueStatus.FAILED)
        store.transition("Q-003", QueueStatus.READY)
        assert _status(store, "Q-003") == QueueStatus.READY.value


class TestInvalidTransitions:
    def test_planned_cannot_skip_to_running(self, store: ResearchStore) -> None:
        """PLANNED → RUNNING is forbidden; must pass through READY."""
        proto = _protocol(protocol_id="R04.TEST")
        store.register(proto, _version(proto), "Q-004", priority=1)

        with pytest.raises(ValueError, match="invalid"):
            store.transition("Q-004", QueueStatus.RUNNING)

    def test_blocked_data_cannot_force_to_running(self, store: ResearchStore) -> None:
        """A data-blocked item must NOT be forced directly into execution."""
        proto = _protocol(protocol_id="R05.TEST")
        store.register(proto, _version(proto), "Q-005", priority=1)
        store.transition("Q-005", QueueStatus.BLOCKED_DATA)

        with pytest.raises(ValueError, match="invalid"):
            store.transition("Q-005", QueueStatus.RUNNING)

    def test_succeeded_is_terminal(self, store: ResearchStore) -> None:
        """Once SUCCEEDED, no further transitions are allowed."""
        proto = _protocol(protocol_id="R06.TEST")
        store.register(proto, _version(proto), "Q-006", priority=1)
        store.transition("Q-006", QueueStatus.READY)
        store.transition("Q-006", QueueStatus.RUNNING)
        store.transition("Q-006", QueueStatus.SUCCEEDED)

        for target in QueueStatus:
            with pytest.raises(ValueError):
                store.transition("Q-006", target)

    def test_cancelled_is_terminal(self, store: ResearchStore) -> None:
        proto = _protocol(protocol_id="R07.TEST")
        store.register(proto, _version(proto), "Q-007", priority=1)
        store.transition("Q-007", QueueStatus.CANCELLED)

        for target in QueueStatus:
            with pytest.raises(ValueError):
                store.transition("Q-007", target)


# ---------------------------------------------------------------------------
# Registration idempotency
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_duplicate_register_is_idempotent(self, store: ResearchStore) -> None:
        """Registering the same queue item twice must not duplicate it."""
        proto = _protocol(protocol_id="R08.TEST")
        version = _version(proto)
        store.register(proto, version, "Q-008", priority=1)
        store.register(proto, version, "Q-008", priority=1)

        assert _status(store, "Q-008") == QueueStatus.PLANNED.value

    def test_protocol_version_mismatch_rejected(self, store: ResearchStore) -> None:
        proto = _protocol(protocol_id="R09.TEST")
        bad_version = StrategyVersion(
            strategy_version_id="sv-bad",
            family_id=proto.family_id,
            protocol_hash=_C_HASH,  # does NOT match proto
            source_code_hash=proto.source_code_hash,
            snapshot_hash=_B_HASH,
            universe_version=proto.universe_version,
        )
        with pytest.raises(ValueError, match="mismatch"):
            store.register(proto, bad_version, "Q-009", priority=1)


# ---------------------------------------------------------------------------
# Stage09 recording
# ---------------------------------------------------------------------------


class TestRecordStage09:
    def test_requires_succeeded_status(self, store: ResearchStore) -> None:
        """Stage09 cannot be recorded for a non-SUCCEEDED item."""
        proto = _protocol(protocol_id="R10.TEST")
        store.register(proto, _version(proto), "Q-010", priority=1)
        store.transition("Q-010", QueueStatus.READY)

        result = Stage09Result(
            rating=Stage09Rating.RESEARCH,
            reasons=("test",),
            protocol_hash=proto.protocol_hash,
            snapshot_hash=_B_HASH,
            primary_metric=Decimal("0.05"),
            effective_trades=10,
            validation_blocks=3,
        )
        with pytest.raises(ValueError, match="successfully"):
            store.record_stage09("s09-1", "Q-010", result)
