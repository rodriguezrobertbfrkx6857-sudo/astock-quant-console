"""Tests for the dependency-assessment module.

Proves correctness of:
- Date parsing helpers (``_as_date``, ``_covers``, ``_protocol_range``).
- Dependency-set resolution for known and unknown strategy families.
- End-to-end ``assess_protocol``: ready when all manifests are present,
  fail-closed when publications are missing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path


from src.quantcore.research.dependencies import (
    _as_date,
    _covers,
    _protocol_range,
    assess_protocol,
    dependency_set,
)


# ---------------------------------------------------------------------------
# Pure helpers (no database needed)
# ---------------------------------------------------------------------------


class TestAsDate:
    def test_compact_format(self) -> None:
        assert _as_date("20200101") == date(2020, 1, 1)

    def test_iso_format(self) -> None:
        assert _as_date("2020-01-01") == date(2020, 1, 1)


class TestCovers:
    """Proves the interval-intersection check used for manifest coverage."""

    def test_overlapping(self) -> None:
        manifest = {"start_date": "2019-01-01", "end_date": "2024-12-31"}
        assert _covers(manifest, date(2020, 1, 1), date(2024, 12, 31)) is True

    def test_before_range(self) -> None:
        manifest = {"start_date": "2019-01-01", "end_date": "2019-12-31"}
        assert _covers(manifest, date(2020, 1, 1), date(2024, 12, 31)) is False

    def test_after_range(self) -> None:
        manifest = {"start_date": "2025-01-01", "end_date": "2025-12-31"}
        assert _covers(manifest, date(2020, 1, 1), date(2024, 12, 31)) is False

    def test_missing_dates_treated_as_covering(self) -> None:
        manifest = {"start_date": None, "end_date": None}
        assert _covers(manifest, date(2020, 1, 1), date(2024, 12, 31)) is True


class TestProtocolRange:
    def test_spans_train_and_validation(self) -> None:
        protocol = {
            "train_window": ["2020-01-01", "2022-12-31"],
            "validation_windows": [
                ["2023-01-01", "2023-06-30"],
                ["2023-07-01", "2024-12-31"],
            ],
        }
        start, end = _protocol_range(protocol)
        assert start == date(2020, 1, 1)
        assert end == date(2024, 12, 31)


class TestDependencySet:
    def test_known_family(self) -> None:
        result = dependency_set({"family_id": "ETF_ROTATION"})
        assert "canonical_prices" in result["REQUIRED"]
        assert "historical_etf_master" in result["REQUIRED"]
        assert "execution_limits" in result["REQUIRED"]

    def test_unknown_family_falls_back_to_protocol_dependencies(self) -> None:
        result = dependency_set(
            {"family_id": "CUSTOM", "data_dependencies": ["canonical_prices"]}
        )
        assert result["REQUIRED"] == ["canonical_prices"]
        assert result["UNUSED"] == []


# ---------------------------------------------------------------------------
# assess_protocol (requires a real SQLite database)
# ---------------------------------------------------------------------------

_SCHEMAS = """
CREATE TABLE data_manifests (
    manifest_id TEXT PRIMARY KEY, manifest_hash TEXT, layer TEXT,
    provenance TEXT, readiness TEXT, manifest_json TEXT
);
CREATE TABLE data_publications (dataset TEXT PRIMARY KEY, manifest_id TEXT);
CREATE TABLE security_state_versions (
    asset_id TEXT, version INTEGER, state_json TEXT
);
"""


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMAS)
    conn.close()


def _insert_manifest(
    conn: sqlite3.Connection,
    dataset: str,
    manifest_id: str,
    start: str,
    end: str,
) -> None:
    manifest_json = json.dumps(
        {"start_date": start, "end_date": end, "row_count": 100, "source_id": "test"}
    )
    conn.execute(
        "INSERT INTO data_manifests VALUES (?,?,?,?,?,?)",
        (manifest_id, "hash-" + manifest_id, "CURATED", "REAL", "REAL_READY", manifest_json),
    )
    conn.execute(
        "INSERT INTO data_publications VALUES (?,?)",
        (dataset, manifest_id),
    )


def _etf_rotation_protocol() -> dict:
    return {
        "family_id": "ETF_ROTATION",
        "data_dependencies": ["canonical_prices", "historical_etf_master"],
        "train_window": ["2020-01-01", "2022-12-31"],
        "validation_windows": [
            ["2023-01-01", "2023-06-30"],
            ["2023-07-01", "2023-12-31"],
            ["2024-01-01", "2024-12-31"],
        ],
    }


class TestAssessProtocol:
    """Proves that assess_protocol is fail-closed when data is missing."""

    def test_all_present_returns_ready(self, tmp_path: Path) -> None:
        """When every dependency has a REAL_READY manifest, assessment passes."""
        from src.quantcore.contracts.safety import PathBoundary

        db = tmp_path / "ctrl.sqlite"
        _make_db(db)
        conn = sqlite3.connect(db)
        _insert_manifest(conn, "research_etf3_prices", "p1", "2019-01-01", "2024-12-31")
        _insert_manifest(conn, "historical_etf_master", "m1", "2019-01-01", "2024-12-31")
        _insert_manifest(conn, "research_etf3_limits", "l1", "2020-01-01", "2024-12-31")
        conn.commit()
        conn.close()

        original_require_readable = PathBoundary.require_readable
        PathBoundary.require_readable = lambda self, p: Path(p)
        try:
            result = assess_protocol(db, _etf_rotation_protocol())
        finally:
            PathBoundary.require_readable = original_require_readable

        assert result.ready is True
        assert result.missing == ()

    def test_missing_publications_returns_blocked(self, tmp_path: Path) -> None:
        """An empty database must fail closed — nothing is ready."""
        from src.quantcore.contracts.safety import PathBoundary

        db = tmp_path / "ctrl.sqlite"
        _make_db(db)

        original_require_readable = PathBoundary.require_readable
        PathBoundary.require_readable = lambda self, p: Path(p)
        try:
            result = assess_protocol(db, _etf_rotation_protocol())
        finally:
            PathBoundary.require_readable = original_require_readable

        assert result.ready is False
        assert len(result.missing) > 0
        assert "canonical_prices" in result.missing
