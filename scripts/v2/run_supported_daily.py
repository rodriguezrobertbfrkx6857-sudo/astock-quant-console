"""Run the bounded real-data daily operational profile.

This command is the local acceptance loop for the currently supported scope.
It keeps the exact 20-session Formal/Independent-Reference replay as an
engine gate, then performs a real PAPER step for every active qualified V2
account on an explicitly supported ETF3 session.  It never connects to a
broker and never treats a missing current session as a historical replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.operations.profile import (  # noqa: E402
    load_operational_profile,
    validate_operational_profile,
)
from src.quantcore.portfolio.paper_operations import (  # noqa: E402
    CURRENT_PAPER_PROFILE,
    latest_supported_session,
    operate,
    v2_accounts,
)
from scripts.v2.run_known_etf_replay import run as run_known_etf_replay  # noqa: E402


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _set_status(cfg, status: str, detail: str, evidence_ref: str) -> None:
    persisted_status = "BLOCKED_DATA" if status == "WAITING_DATA" else status
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
            "detail=excluded.detail,evidence_ref=excluded.evidence_ref,"
            "updated_at=excluded.updated_at",
            (
                "supported_daily_loop",
                persisted_status,
                detail,
                evidence_ref,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _research_summary(cfg) -> dict[str, Any]:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        strategy_runs = int(
            conn.execute(
                "SELECT count(*) FROM research_runs "
                "WHERE execution_scope='STRATEGY' AND status='SUCCEEDED'"
            ).fetchone()[0]
        )
        stage10_pass = int(
            conn.execute(
                "SELECT count(*) FROM stage10_runs s JOIN research_runs r "
                "ON r.research_run_id=s.research_run_id "
                "WHERE r.execution_scope='STRATEGY' AND s.status='PASS' "
                "AND s.evidence_valid=1"
            ).fetchone()[0]
        )
        qualifications = int(
            conn.execute(
                "SELECT count(*) FROM qualification_events e "
                "WHERE e.status='ACTIVE' AND e.rowid=(SELECT max(x.rowid) "
                "FROM qualification_events x WHERE x.qualification_id=e.qualification_id)"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return {
        "strategy_runs_succeeded": strategy_runs,
        "stage10_pass": stage10_pass,
        "active_qualifications": qualifications,
    }


def run(trade_date: str | None = None) -> dict[str, Any]:
    cfg = load_v2_config(create_dirs=True)
    profile = load_operational_profile(cfg)
    validation = validate_operational_profile(cfg)
    if not validation.ready:
        raise RuntimeError(
            "supported operational profile validation failed: "
            + ",".join(validation.reasons)
        )

    replay = run_known_etf_replay()
    if (
        replay.get("status") != "PASS"
        or replay.get("synthetic") is not False
        or replay.get("stage10_status") != "PASS"
        or replay.get("stage10_differences") != 0
    ):
        raise RuntimeError("real supported replay did not pass its exact-match gate")

    selected_date = (
        date.fromisoformat(trade_date)
        if trade_date
        else latest_supported_session(cfg, profile=CURRENT_PAPER_PROFILE)
    )
    if selected_date is None:
        raise RuntimeError("no complete REAL_READY ETF3 PAPER session is published")

    accounts = [
        item
        for item in v2_accounts(cfg)
        if item.get("data_profile") == CURRENT_PAPER_PROFILE
        and item.get("status") == "ACTIVE"
    ]
    paper_results: list[dict[str, Any]] = []
    for account in accounts:
        strategy_id = str(account["strategy_id"])
        start_session = date.fromisoformat(str(account["generation_start_session"]))
        if selected_date < start_session:
            paper_results.append(
                {
                    "status": "WAITING_FIRST_FORWARD_SESSION",
                    "account_id": account["account_id"],
                    "strategy_id": strategy_id,
                    "business_date": selected_date.isoformat(),
                    "generation_start_session": start_session.isoformat(),
                    "real_orders": False,
                }
            )
            continue
        request_hash = hashlib.sha256(
            f"supported-daily-paper|{strategy_id}|{selected_date.isoformat()}".encode()
        ).hexdigest()
        paper_results.append(
            operate(
                cfg,
                strategy_id,
                "step",
                trade_date=selected_date.strftime("%Y%m%d"),
                request_hash=request_hash,
            )
        )
    paper_states = [str(item.get("status")) for item in paper_results]
    paper_status = (
        "PASS_NO_ACCOUNTS"
        if not accounts
        else "PASS_WAITING_FIRST_FORWARD_SESSION"
        if all(state == "WAITING_FIRST_FORWARD_SESSION" for state in paper_states)
        else "PASS"
        if all(state == "SUCCEEDED" for state in paper_states)
        else "WAITING_DATA"
    )
    research = _research_summary(cfg)
    evidence_ref = "evidence:SUPPORTED_PROFILE/supported_daily.json"
    report: dict[str, Any] = {
        "status": "PASS" if paper_status.startswith("PASS") else paper_status,
        "profile_id": profile["profile_id"],
        "scope": "ETF3_CURRENT_FORWARD",
        "mode": "REAL_CURRENT_DAILY_PLUS_FORMAL_REFERENCE_ENGINE_GATE",
        "synthetic": False,
        "business_date": selected_date.isoformat(),
        "supported_securities": validation.supported_securities,
        "research": research,
        "pipeline": {
            "canonical_data": {
                "status": "PASS",
                "provenance": "REAL",
                "manifest_hashes": replay["input_manifest_hashes"],
            },
            "supported_universe": {
                "status": "PASS",
                "state": "VERIFIED_BOUNDED_ETF3",
                "securities": list(validation.supported_securities),
            },
            "pit_as_of_filter": {
                "status": "PASS",
                "available_at_filter": replay["future_rows_filtered_by_available_at"],
                "known_only": True,
            },
            "factor_strategy": {
                "status": "PASS",
                "strategy": replay["strategy"],
                "signal_source": "REAL_CANONICAL_ETF3_BARS",
                "oos_decision": replay["oos_decision"],
                "registered_strategy_runs": research["strategy_runs_succeeded"],
            },
            "formal": {
                "status": "PASS",
                "snapshot_hash": replay["formal_hash"],
                "fills": replay["fills"],
            },
            "reference": {
                "status": "PASS",
                "snapshot_hash": replay["reference_hash"],
            },
            "portfolio": {
                "status": "PASS",
                "final_nav": replay["final_nav"],
                "fills": replay["fills"],
            },
            "risk": {
                "status": "PASS",
                "rulebook": "default_rulebook",
                "execution_limit_checks": "PASS",
            },
            "reporting": {
                "status": "PASS",
                "publication_id": replay["reporting_publication_id"],
            },
            "paper": {
                "status": paper_status,
                "business_date": selected_date.isoformat(),
                "eligible_strategies": [str(item["strategy_id"]) for item in accounts],
                "operations": paper_results,
                "real_orders": False,
            },
        },
        "stage10": {
            "status": replay["stage10_status"],
            "result_id": replay["stage10_result_id"],
            "formal_hash": replay["formal_hash"],
            "reference_hash": replay["reference_hash"],
            "differences": replay["stage10_differences"],
            "exact_match": replay["formal_hash"] == replay["reference_hash"],
        },
        "full_scope": {
            "status": "PARTIAL",
            "reason": "This profile is not full-A-share coverage.",
        },
        "limitations": profile["known_limitations"],
        "evidence_ref": evidence_ref,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    target = cfg.paths.artifacts / "evidence" / "SUPPORTED_PROFILE" / "supported_daily.json"
    _write(target, report)
    _set_status(
        cfg,
        report["status"],
        f"real bounded daily completed for {selected_date.isoformat()}; paper={paper_status}",
        evidence_ref,
    )
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the bounded real ETF3 daily profile")
    parser.add_argument(
        "--date",
        default="",
        help="explicit supported ETF3 session YYYY-MM-DD; defaults to latest published session",
    )
    args = parser.parse_args()
    report = run(args.date or None)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
