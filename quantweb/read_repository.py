"""SQLite query repository that cannot create or mutate database files."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

from src.quantcore.contracts.safety import PathBoundary, SafetyViolation


_DENIED_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_CREATE_INDEX", -1),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", -1),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", -1),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", -1),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", -1),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", -1),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", -1),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", -1),
        getattr(sqlite3, "SQLITE_DELETE", -1),
        getattr(sqlite3, "SQLITE_DROP_INDEX", -1),
        getattr(sqlite3, "SQLITE_DROP_TABLE", -1),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", -1),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", -1),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", -1),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", -1),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", -1),
        getattr(sqlite3, "SQLITE_DROP_VIEW", -1),
        getattr(sqlite3, "SQLITE_INSERT", -1),
        getattr(sqlite3, "SQLITE_UPDATE", -1),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", -1),
        getattr(sqlite3, "SQLITE_ATTACH", -1),
        getattr(sqlite3, "SQLITE_DETACH", -1),
        getattr(sqlite3, "SQLITE_REINDEX", -1),
        getattr(sqlite3, "SQLITE_ANALYZE", -1),
    )
    if action >= 0
)


def _read_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _db: str | None,
    _source: str | None,
) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK


class OfflineSQLiteRepository:
    """Open existing SQLite files under an approved root in immutable mode.

    There is deliberately no migration or DDL hook in this repository. Missing
    files fail instead of being created, and SQLite's authorizer independently
    rejects mutations even if a caller sends one through a query method.
    """

    def __init__(self, root: str | Path, *, boundary: PathBoundary | None = None):
        self._boundary = boundary or PathBoundary()
        self.root = self._boundary.require_readable(root)

    def database_path(self, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise SafetyViolation("database filename must not contain a path")
        target = self._boundary.require_readable(self.root / filename)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise SafetyViolation("database path escapes repository root") from exc
        if not target.is_file():
            raise FileNotFoundError(f"read-only database does not exist: {target}")
        return target

    def connect(self, filename: str) -> sqlite3.Connection:
        target = self.database_path(filename)
        self._reject_uncheckpointed_sidecars(target)
        encoded = quote(target.as_posix(), safe="/:")
        conn = sqlite3.connect(
            f"file:{encoded}?mode=ro&immutable=1",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_read_authorizer)
        try:
            self._reject_uncheckpointed_sidecars(target)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _reject_uncheckpointed_sidecars(target: Path) -> None:
        # immutable=1 intentionally never touches WAL/journal files. A non-empty
        # sidecar would therefore make the visible snapshot incomplete.
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(target) + suffix)
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                raise SafetyViolation(
                    f"read-only snapshot has uncheckpointed sidecar: {sidecar.name}"
                )

    @staticmethod
    def all(
        conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()
    ) -> list[sqlite3.Row]:
        return conn.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def one(
        conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()
    ) -> sqlite3.Row | None:
        return conn.execute(sql, tuple(params)).fetchone()

    @staticmethod
    def iter_rows(
        conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()
    ) -> Iterable[sqlite3.Row]:
        yield from conn.execute(sql, tuple(params))
