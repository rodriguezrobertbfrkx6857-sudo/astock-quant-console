from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .safety import PathBoundary


def migration_plan(
    database: Path, sql_path: Path, version: str, required_version: str
) -> dict[str, object]:
    database = PathBoundary().require_writable(database)
    if not database.is_file():
        raise RuntimeError("control.sqlite is required")
    checksum = hashlib.sha256(sql_path.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        if not conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (required_version,)
        ).fetchone():
            raise RuntimeError(f"required migration missing: {required_version}")
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
    finally:
        conn.close()
    if row and row[0] != checksum:
        raise RuntimeError("applied migration checksum mismatch")
    return {
        "version": version,
        "target": str(database),
        "checksum": checksum,
        "status": "ALREADY_APPLIED" if row else "PENDING",
        "legacy_mutations": 0,
        "sql_path": str(sql_path),
    }


def apply_migration(report: dict[str, object], sql_path: Path) -> None:
    if report["status"] == "ALREADY_APPLIED":
        return
    conn = sqlite3.connect(str(report["target"]))
    try:
        stamp = datetime.now(UTC).isoformat().replace("'", "''")
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + sql_path.read_text("utf-8")
            + f"\nINSERT INTO schema_migrations(version,checksum,applied_at) VALUES ('{report['version']}','{report['checksum']}','{stamp}');\nCOMMIT;"
        )
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
