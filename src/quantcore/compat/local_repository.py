"""Mutation-denying SQLite repository for the local evidence archive."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

from src.quantcore.contracts.safety import PROJECT_ROOT, PathBoundary, SafetyViolation


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


def _authorizer(action: int, *_args: object) -> int:
    return sqlite3.SQLITE_DENY if action in _DENIED_ACTIONS else sqlite3.SQLITE_OK


class LocalSQLiteRepository:
    """Open only databases inside the published local archive."""

    def __init__(self, root: str | Path, *, boundary: PathBoundary | None = None):
        self._boundary = boundary or PathBoundary()
        published = self._boundary.require_readable(root)
        if not published.is_relative_to(PROJECT_ROOT):
            raise SafetyViolation("local repository must stay inside the main project")
        # The configured archive path is the atomic publication directory,
        # not the version directory itself. Resolve its pointer once so the
        # legacy-compatible routes can use the same published layout as V2.
        pointer_path = published / "current.json"
        if pointer_path.is_file():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                if pointer.get("status") != "PUBLISHED":
                    raise SafetyViolation("local archive publication is not PUBLISHED")
                version = (published / str(pointer["version_path"])).resolve(strict=True)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise SafetyViolation("invalid local archive publication pointer") from exc
            if not version.is_dir() or not version.is_relative_to(PROJECT_ROOT):
                raise SafetyViolation("local archive version escapes the main project")
            self.root = version
        else:
            self.root = published

    def database_path(self, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise SafetyViolation("database filename must not contain a path")
        target = self._boundary.require_readable(self.root / filename)
        if not target.is_relative_to(self.root):
            raise SafetyViolation("database path escapes local repository")
        if not target.is_file():
            raise FileNotFoundError(target)
        return target

    def connect(self, filename: str) -> sqlite3.Connection:
        target = self.database_path(filename)
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(target) + suffix)
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                raise SafetyViolation(f"local archive has non-empty sidecar: {sidecar.name}")
        encoded = quote(target.resolve().as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{encoded}?mode=ro&immutable=1",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorizer)
        return connection

    @staticmethod
    def all(connection: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return connection.execute(sql, tuple(params)).fetchall()

    @staticmethod
    def one(connection: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        return connection.execute(sql, tuple(params)).fetchone()

    @staticmethod
    def iter_rows(connection: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> Iterable[sqlite3.Row]:
        yield from connection.execute(sql, tuple(params))
