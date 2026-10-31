"""Initialize empty local schema; no network, imported data, or account state."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.quantcore.contracts.config import load_v2_config


def initialize(database: Path, migrations: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.commit()
        for sql in sorted(migrations.glob("*.sql")):
            checksum = hashlib.sha256(sql.read_bytes()).hexdigest()
            applied = conn.execute("SELECT checksum FROM schema_migrations WHERE version=?", (sql.stem,)).fetchone()
            if applied:
                if applied[0] != checksum:
                    raise RuntimeError(f"Migration checksum mismatch: {sql.name}")
                continue
            stamp = datetime.now(timezone.utc).isoformat()
            try:
                conn.executescript("BEGIN IMMEDIATE;\n" + sql.read_text("utf-8") +
                    f"\nINSERT INTO schema_migrations VALUES ('{sql.stem}','{checksum}','{stamp}');\nCOMMIT;")
            except Exception:
                conn.rollback()
                raise


def main() -> None:
    cfg = load_v2_config(create_dirs=True)
    initialize(cfg.paths.state / "control.sqlite", ROOT / "src/quantcore/data/migrations")
    initialize(cfg.paths.state / "accounting.sqlite", ROOT / "src/quantcore/accounting/migrations")
    print("Local schema ready. No market data, credentials or qualified accounts were imported.")


if __name__ == "__main__":
    main()
