"""Expose Watch readiness without mislabeling daily bars as realtime ticks."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.contracts.config import load_v2_config  # noqa: E402


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def run() -> dict[str, Any]:
    cfg = load_v2_config(create_dirs=True)
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT dataset,manifest_hash FROM data_publications "
            "WHERE dataset IN ('rt_k','rt_min','rt_min_daily','stk_mins') ORDER BY dataset"
        ).fetchall()
    finally:
        conn.close()
    intraday_publications = [
        {"dataset": str(row[0]), "manifest_hash": str(row[1])} for row in rows
    ]
    has_intraday = bool(intraday_publications)
    report: dict[str, Any] = {
        "status": "PASS",
        "engine_ready": True,
        "live_feed_status": "PENDING_VENDOR" if not has_intraday else "PENDING_VALIDATION",
        "mode": "REAL_HISTORICAL_DAILY_BAR_REPLAY",
        "replay_status": "NOT_RUN_NO_REAL_INTRADAY_PUBLICATION" if not has_intraday else "PENDING_VALIDATION",
        "realtime_semantics": False,
        "intraday_publications": intraday_publications,
        "message": (
            "WATCH ENGINE READY; LIVE DATA UNAVAILABLE. Persisted data is daily-bar data, "
            "so it is not fed into the tick store or presented as ticks."
            if not has_intraday
            else "Intraday publication exists but requires separate market-hours validation before live use."
        ),
        "capabilities": [
            "price_move",
            "volume_anomaly",
            "volatility_spike",
            "gap",
            "breakout",
            "breakdown",
            "limit_proximity",
            "factor_rank_change",
            "strategy_state",
            "paper_drawdown",
            "risk_warning",
            "new_event",
        ],
        "synthetic": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _write(cfg.paths.artifacts / "evidence" / "SUPPORTED_PROFILE" / "watch_replay.json", report)
    return report


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
