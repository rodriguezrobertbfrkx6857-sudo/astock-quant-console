"""Register and assess the frozen short-term families inside the existing KNOWN window."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.research.oos import AccessPurpose, OOSAccessService  # noqa: E402

FAMILIES = (
    "SHORT_TREND_PULLBACK_V1",
    "SHORT_BREAKOUT_CONFIRM_V1",
    "SHORT_REVERSAL_V1",
    "BASE_T_CONDITIONAL_V1",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict[str, object]:
    cfg = load_v2_config(create_dirs=True)
    protocol = ROOT / "SHORT_TERM_RESEARCH_PROTOCOL.md"
    source = ROOT / "src" / "quantcore" / "short_term" / "workbench.py"
    protocol_hash = _sha(protocol)
    source_hash = _sha(source)
    parquet_files = sorted((cfg.paths.data / "market_store" / "versions").rglob("dataset=daily/**/*.parquet"))
    snapshot_hash = hashlib.sha256("".join(_sha(path) for path in parquet_files).encode("ascii")).hexdigest()
    service = OOSAccessService(cfg.paths.state / "control.sqlite")
    receipts = []
    for family in FAMILIES:
        receipt = service.authorize_known(
            protocol_hash=protocol_hash,
            snapshot_hash=snapshot_hash,
            start=date(2025, 1, 2),
            end=date(2025, 8, 18),
            purpose=AccessPurpose.RESEARCH,
            asset_scope="GLOBAL",
            family_scope=family,
        )
        receipts.append({"family": family, "receipt_id": receipt.receipt_id, "decision": receipt.decision.value, "reason": receipt.reason})

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for family in FAMILIES:
            conn.execute("INSERT OR IGNORE INTO strategy_families(family_id,control_only) VALUES (?,0)", (family,))
            conn.execute(
                "INSERT OR IGNORE INTO strategy_versions(strategy_version_id,family_id,protocol_hash,source_code_hash,snapshot_hash,universe_version,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"{family}.engineering.v1", family, protocol_hash, source_hash, snapshot_hash, "PIT_UNIVERSE_REQUIRED_BLOCKED", datetime.now(UTC).isoformat()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "BLOCKED_DATA",
        "protocol_hash": protocol_hash,
        "source_code_hash": source_hash,
        "snapshot_hash": snapshot_hash,
        "authorized_window": {"start": "2025-01-02", "end": "2025-08-18", "classification": "KNOWN_ONLY"},
        "receipts": receipts,
        "families": [
            {"family": family, "registry_status": "REGISTERED_ENGINEERING_ONLY", "research_status": "BLOCKED_DATA", "paper_qualification": "NONE", "reason": "historical PIT stock identity/universe membership and corporate-action-complete labels are not available in a certified snapshot" if family != "BASE_T_CONDITIONAL_V1" else "certified historical minute bars and intraminute ordering evidence are unavailable"}
            for family in FAMILIES
        ],
        "metrics_computed": False,
        "parameters_changed": False,
        "untouched_oos_consumed": False,
        "etf_qualification_inherited": False,
        "decision": "No family may advance; engineering-only current observation plans remain visibly blocked/degraded.",
    }
    target = cfg.paths.artifacts / "evidence" / "SHORT_TERM" / "research_validation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, target)
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
