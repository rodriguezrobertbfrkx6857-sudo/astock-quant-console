from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.activation.data_worker import CORE_DATASETS, sync_core  # noqa: E402
from src.quantcore.activation.operations import (  # noqa: E402
    _heartbeat,
    register_daily,
    register_research,
    run_until_idle,
)
from src.quantcore.activation.status import (  # noqa: E402
    current_business_date,
    published_trade_calendar,
)
from src.quantcore.activation.vendor import RelayClient  # noqa: E402
from src.quantcore.activation.schedule import status_at  # noqa: E402
from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.contracts.safety import validate_runtime_safety  # noqa: E402


def _write(name: str, payload: dict[str, object]) -> None:
    cfg = load_v2_config()
    target = cfg.paths.artifacts / "evidence" / "FULL_ACTIVATION" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    os.replace(temporary, target)


def _write_supported(name: str, payload: dict[str, object]) -> None:
    cfg = load_v2_config()
    target = cfg.paths.artifacts / "evidence" / "SUPPORTED_PROFILE" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    os.replace(temporary, target)


def _update_scheduler_status(cfg) -> dict[str, object]:
    feed_status = "PENDING_VENDOR" if not RelayClient.configured() else "WATCH_DEGRADED_VENDOR"
    calendar, calendar_manifest_hash = published_trade_calendar(cfg)
    status = status_at(
        feed_status=feed_status,
        trade_calendar=calendar,
        calendar_manifest_hash=calendar_manifest_hash,
    )
    payload = status.as_dict()
    calendar_known = bool(payload["calendar"]["known"])
    payload["scheduler_status"] = (
        "BLOCKED_CALENDAR"
        if not calendar_known
        else
        "WATCH_DEGRADED_VENDOR"
        if status.watch_enabled and feed_status != "PASS"
        else "PASS"
    )
    payload["generated_at"] = datetime.now(UTC).isoformat()
    _write_supported("scheduler_status.json", payload)
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        persisted_status = "PASS" if payload["scheduler_status"] == "PASS" else (
            "BLOCKED_DATA" if payload["scheduler_status"] == "BLOCKED_CALENDAR" else "DEGRADED"
        )
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
            "detail=excluded.detail,evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            (
                "scheduler",
                persisted_status,
                f"phase={payload['phase']}; watch_enabled={payload['watch_enabled']}; "
                f"feed={feed_status}; calendar={payload['calendar']['source']}; "
                f"calendar_date_known={payload['calendar']['known']}",
                "evidence:SUPPORTED_PROFILE/scheduler_status.json",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return payload


def doctor() -> dict[str, object]:
    cfg = load_v2_config(create_dirs=True)
    validate_runtime_safety(host=cfg.server.host)
    free = shutil.disk_usage(ROOT).free
    checks = {
        "safety": "PASS",
        "host": "PASS" if cfg.server.host == "127.0.0.1" else "FAIL",
        "database": "PASS"
        if (cfg.paths.state / "control.sqlite").is_file()
        else "FAIL",
        "disk": "PASS" if free >= cfg.resources.minimum_free_bytes else "FAIL",
        "vendor": "PRESENT" if RelayClient.configured() else "PENDING_VENDOR",
    }
    report = {
        "command": "doctor",
        "checks": checks,
        "free_bytes": free,
        "generated_at": datetime.now(UTC).isoformat(),
        "secrets_emitted": False,
    }
    _write("doctor.json", report)
    return report


def status() -> dict[str, object]:
    cfg = load_v2_config()
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        jobs = dict(
            conn.execute("SELECT status,count(*) FROM runtime_jobs GROUP BY status")
        )
        manifests = conn.execute(
            "SELECT count(*) FROM data_manifests WHERE provenance='REAL' AND readiness='REAL_READY' AND layer='CURATED'"
        ).fetchone()[0]
        operations = [
            dict(
                zip(
                    ("component", "status", "detail", "evidence_ref", "updated_at"), row
                )
            )
            for row in conn.execute(
                "SELECT component,status,detail,evidence_ref,updated_at FROM operational_status ORDER BY component"
            )
        ]
    finally:
        conn.close()
    return {
        "runtime_jobs": jobs,
        "real_curated_manifests": manifests,
        "operations": operations,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _mark_runtime_cycle_error(cfg, pid: int, error: Exception) -> None:
    """Keep the supervisor heartbeat truthful after a recoverable cycle error."""

    now = datetime.now(UTC).isoformat()
    detail = f"cycle_error={type(error).__name__}; worker remains local-only and will retry"
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        for worker_id, worker_type in (
            (f"runtime-daily-{pid}", "DAILY"),
            (f"runtime-research-{pid}", "RESEARCH"),
        ):
            conn.execute(
                "INSERT INTO worker_heartbeats VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(worker_id) DO UPDATE SET worker_type=excluded.worker_type,"
                "status=excluded.status,process_id=excluded.process_id,detail=excluded.detail,"
                "heartbeat_at=excluded.heartbeat_at",
                (worker_id, worker_type, "DEGRADED", pid, detail, now),
            )
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,detail=excluded.detail,"
            "evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            (
                "runtime_loop",
                "DEGRADED",
                detail,
                "evidence:FULL_ACTIVATION/runtime_smoke.json",
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Integrated V2 local-only operational activation"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    data = sub.add_parser("data")
    data.add_argument("--date")
    data.add_argument(
        "--datasets",
        default="trade_cal,stock_basic,daily,adj_factor,daily_basic,stk_limit",
    )
    eod = sub.add_parser("eod")
    eod.add_argument("--date", default=date.today().isoformat())
    research = sub.add_parser("research")
    research.add_argument("--date", default=date.today().isoformat())
    runtime = sub.add_parser("runtime")
    runtime.add_argument("--once", action="store_true")
    runtime.add_argument("--interval", type=int, default=60)
    sub.add_parser("status")
    args = parser.parse_args()
    cfg = load_v2_config(create_dirs=True)
    validate_runtime_safety(host=cfg.server.host)
    if args.command == "doctor":
        result = doctor()
    elif args.command == "data":
        selected = tuple(x.strip() for x in args.datasets.split(",") if x.strip())
        unknown = sorted(set(selected) - set(CORE_DATASETS))
        if unknown:
            raise SystemExit(f"unknown datasets: {','.join(unknown)}")
        result = {"results": sync_core(cfg, business_date=args.date, datasets=selected)}
        _write("data_worker.json", result)
    elif args.command == "eod":
        day = date.fromisoformat(args.date)
        keys = register_daily(cfg, day)
        processed = run_until_idle(cfg, "DAILY", f"daily-{os.getpid()}")
        result = {
            "registered": len(keys),
            "processed": processed,
            "business_date": args.date,
            "status": status(),
        }
        _write("eod_smoke.json", result)
    elif args.command == "research":
        day = date.fromisoformat(args.date)
        keys = register_research(cfg, day)
        processed = run_until_idle(cfg, "RESEARCH", f"research-{os.getpid()}")
        result = {
            "registered": len(keys),
            "processed": processed,
            "business_date": args.date,
            "status": status(),
        }
        _write("research_smoke.json", result)
    elif args.command == "runtime":
        cycles = 0
        while True:
            try:
                # The same supervised Runtime owns the Daily and Research
                # queues.  Advertise both roles before a long Daily batch so
                # health checks do not mistake legitimate queue work for a
                # missing Research worker.
                pid = os.getpid()
                _heartbeat(cfg, f"runtime-daily-{pid}", "DAILY", "IDLE", "cycle starting")
                _heartbeat(cfg, f"runtime-research-{pid}", "RESEARCH", "IDLE", "cycle starting")
                _update_scheduler_status(cfg)
                # Current Daily is bound to the current A-share business date.
                # ``latest_market_session`` is historical EOD truth and must
                # not be reused as today's run date.
                current_day = current_business_date(cfg)
                if current_day is not None:
                    register_daily(cfg, current_day)
                else:
                    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
                    try:
                        conn.execute(
                            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
                            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
                            "detail=excluded.detail,evidence_ref=excluded.evidence_ref,"
                            "updated_at=excluded.updated_at",
                            (
                                "daily_loop",
                                "BLOCKED_DATA",
                                "trade calendar publication is unavailable; current business date unresolved",
                                "evidence:FULL_ACTIVATION/runtime_smoke.json",
                                datetime.now(UTC).isoformat(),
                            ),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                register_research(cfg, date.today())
                run_until_idle(cfg, "DAILY", f"runtime-daily-{pid}")
                run_until_idle(cfg, "RESEARCH", f"runtime-research-{pid}")
            except Exception as exc:  # noqa: BLE001 - supervisor must stay alive and retry finitely
                _mark_runtime_cycle_error(cfg, os.getpid(), exc)
                if args.once:
                    break
                time.sleep(min(max(10, args.interval), 30))
            cycles += 1
            if args.once:
                break
            time.sleep(max(10, args.interval))
        result = {"cycles": cycles, "status": status()}
        _write("runtime_smoke.json", result)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
