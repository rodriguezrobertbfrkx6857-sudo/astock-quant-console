"""Shared operational-date and run-truth helpers for Integrated V2.

The UI, runtime and closure evidence must answer two different questions:
which historical EOD session is complete, and which business date the system is
currently trying to process.  This module keeps those questions separate and
derives the current Daily status from the persisted DAG instead of from an old
supported-profile replay.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.quantcore.contracts.config import V2Config


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRICE_DAILY_DATASETS = ("daily", "adj_factor", "stk_limit")
DAILY_PIPELINE_JOBS = (
    "data.sync.daily",
    "data.sync.adj_factor",
    "data.sync.stk_limit",
    "daily.data_quality",
    "daily.universe",
    "daily.pit",
    "daily.factors",
    "daily.operational_strategies",
    "daily.portfolio_risk",
    "daily.paper",
    "daily.report",
    "daily.dashboard_publish",
)


def _manifest_payload(cfg: V2Config, conn: sqlite3.Connection, dataset: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT m.manifest_json FROM data_publications p "
        "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
        "WHERE p.dataset=? AND m.readiness='REAL_READY' AND m.provenance='REAL'",
        (dataset,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_rows(cfg: V2Config, payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = cfg.paths.data / "raw" / "objects" / "sha256"
    rows: list[dict[str, Any]] = []
    for digest in payload.get("object_hashes", []):
        digest = str(digest)
        if len(digest) != 64:
            continue
        path = root / digest[:2] / digest
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


def publication_snapshot(cfg: V2Config) -> str:
    """Hash the current canonical publication pointers, not wall-clock time."""

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = list(
            conn.execute(
                "SELECT dataset,manifest_hash FROM data_publications ORDER BY dataset"
            )
        )
    finally:
        conn.close()
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def latest_complete_market_session(cfg: V2Config) -> date | None:
    """Return the newest common EOD date across all required price datasets."""

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT p.dataset,json_extract(m.manifest_json,'$.end_date') "
            "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            f"WHERE p.dataset IN ({','.join('?' for _ in PRICE_DAILY_DATASETS)}) "
            "AND m.readiness='REAL_READY' AND m.provenance='REAL'",
            PRICE_DAILY_DATASETS,
        ).fetchall()
    finally:
        conn.close()
    ends = {str(dataset): str(end or "") for dataset, end in rows}
    if set(ends) != set(PRICE_DAILY_DATASETS):
        return None
    if not all(len(value) == 8 and value.isdigit() for value in ends.values()):
        return None
    try:
        return datetime.strptime(min(ends.values()), "%Y%m%d").date()
    except ValueError:
        return None


def current_business_date(cfg: V2Config, *, now: datetime | None = None) -> date | None:
    """Resolve today's A-share calendar date without substituting history.

    On a market holiday this returns the latest open calendar date up to today.
    If the local trade calendar is unavailable, ``None`` is returned so callers
    can expose an explicit calendar block instead of silently using ``today``.
    """

    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=SHANGHAI)
    today = observed.astimezone(SHANGHAI).date()
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        payload = _manifest_payload(cfg, conn, "trade_cal")
        if payload is None:
            return None
        rows = _manifest_rows(cfg, payload)
    finally:
        conn.close()
    open_dates = sorted(
        date.fromisoformat(
            f"{str(row['cal_date'])[:4]}-{str(row['cal_date'])[4:6]}-{str(row['cal_date'])[6:8]}"
        )
        for row in rows
        if str(row.get("cal_date", "")).isdigit()
        and len(str(row.get("cal_date", ""))) == 8
        and int(row.get("is_open") or 0) == 1
        and str(row["cal_date"]) <= today.strftime("%Y%m%d")
    )
    return open_dates[-1] if open_dates else None


def published_trade_calendar(cfg: V2Config) -> tuple[dict[date, bool], str | None]:
    """Load the current REAL_READY exchange calendar publication.

    The returned mapping includes both open and closed published dates.  An
    absent date therefore means *unknown*, rather than a weekday fallback.
    The second value is the bound manifest hash for audit/UI display.
    """

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT m.manifest_hash,m.manifest_json FROM data_publications p "
            "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE p.dataset='trade_cal' AND m.readiness='REAL_READY' "
            "AND m.provenance='REAL'"
        ).fetchone()
        if row is None:
            return {}, None
        try:
            payload = json.loads(str(row[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, None
        if not isinstance(payload, dict):
            return {}, None
        rows = _manifest_rows(cfg, payload)
    finally:
        conn.close()

    calendar: dict[date, bool] = {}
    for item in rows:
        raw = str(item.get("cal_date", ""))
        if len(raw) != 8 or not raw.isdigit():
            continue
        try:
            session = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            continue
        calendar[session] = int(item.get("is_open") or 0) == 1
    return calendar, str(row[0]) if calendar else None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def daily_status(cfg: V2Config) -> dict[str, Any]:
    """Derive current Daily truth from the date-bound persisted DAG."""

    business_date = current_business_date(cfg)
    snapshot = publication_snapshot(cfg)
    run_id = (
        f"daily-{business_date.isoformat()}-{snapshot[:16]}"
        if business_date is not None
        else f"daily-unresolved-{snapshot[:16]}"
    )
    scope = "LOCAL_DAILY_EOD"
    if business_date is None:
        latest_complete = latest_complete_market_session(cfg)
        return {
            "status": "BLOCKED_DATA",
            "business_date": None,
            "latest_complete_market_date": latest_complete.isoformat() if latest_complete else None,
            "scope": scope,
            "snapshot": snapshot,
            "run_id": run_id,
            "completed_at": None,
            "job_statuses": {},
            "detail": "trade calendar publication is unavailable; current business date is unresolved",
        }

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT job_type,status,updated_at,checkpoint_json FROM runtime_jobs "
            "WHERE business_date=? AND input_snapshot=? AND loop_kind='DAILY' "
            "AND job_type IN (" + ",".join("?" for _ in DAILY_PIPELINE_JOBS) + ") "
            "ORDER BY job_type",
            (business_date.isoformat(), snapshot, *DAILY_PIPELINE_JOBS),
        ).fetchall()
    finally:
        conn.close()

    job_statuses = {str(row["job_type"]): str(row["status"]) for row in rows}
    missing = [job for job in DAILY_PIPELINE_JOBS if job not in job_statuses]
    failed = [job for job, status in job_statuses.items() if status == "FAILED"]
    waiting = [
        job
        for job, status in job_statuses.items()
        if status in {"WAITING_DATA", "DEGRADED"}
    ]
    running = [job for job, status in job_statuses.items() if status == "RUNNING"]
    pending = [job for job, status in job_statuses.items() if status == "PENDING"]
    if failed:
        status = "FAIL"
    elif waiting:
        status = "BLOCKED_DATA"
    elif missing or pending:
        status = "PENDING"
    elif running:
        status = "RUNNING"
    elif len(job_statuses) == len(DAILY_PIPELINE_JOBS) and all(
        value == "SUCCEEDED" for value in job_statuses.values()
    ):
        status = "PASS"
    else:
        status = "PENDING"

    completed_at: str | None = None
    if status == "PASS":
        parsed_timestamps = [
            _parse_timestamp(str(row["updated_at"]))
            for row in rows
            if row["updated_at"]
        ]
        timestamps: list[datetime] = [value for value in parsed_timestamps if value is not None]
        if timestamps:
            completed_at = max(timestamps).astimezone(UTC).isoformat()
    details: list[str] = []
    if missing:
        details.append("missing=" + ",".join(missing))
    if failed:
        details.append("failed=" + ",".join(failed))
    if waiting:
        details.append("waiting=" + ",".join(waiting))
    if running:
        details.append("running=" + ",".join(running))
    if pending:
        details.append("pending=" + ",".join(pending))
    if not details:
        details.append("all date-bound Daily jobs succeeded")
    latest_complete = latest_complete_market_session(cfg)
    return {
        "status": status,
        "business_date": business_date.isoformat(),
        "latest_complete_market_date": latest_complete.isoformat() if latest_complete else None,
        "scope": scope,
        "snapshot": snapshot,
        "run_id": run_id,
        "completed_at": completed_at,
        "job_statuses": job_statuses,
        "detail": "; ".join(details),
    }


__all__ = [
    "DAILY_PIPELINE_JOBS",
    "PRICE_DAILY_DATASETS",
    "current_business_date",
    "daily_status",
    "latest_complete_market_session",
    "published_trade_calendar",
    "publication_snapshot",
]
