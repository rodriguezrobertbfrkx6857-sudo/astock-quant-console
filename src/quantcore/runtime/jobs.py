from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from src.quantcore.contracts.safety import PathBoundary


class FencingViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class JobSpec:
    job_type: str
    business_date: date
    input_snapshot: str
    engine_version: str
    params: dict[str, object]
    loop_kind: str
    max_attempts: int = 3

    @property
    def params_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @property
    def job_key(self) -> str:
        value = "|".join(
            (
                self.job_type,
                self.business_date.isoformat(),
                self.input_snapshot,
                self.engine_version,
                self.params_hash,
            )
        )
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class JobLease:
    job_key: str
    owner: str
    fencing_token: int
    attempt_no: int
    expires_at: datetime


class RuntimeStore:
    def __init__(self, database: str | Path, clock=lambda: datetime.now(UTC)):
        self.database = PathBoundary().require_writable(database)
        self.clock = clock

    def _connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register(self, spec: JobSpec, dependencies: tuple[str, ...] = ()) -> str:
        if spec.loop_kind not in {"DAILY", "RESEARCH"} or spec.max_attempts <= 0:
            raise ValueError("invalid job spec")
        now = self.clock().isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            values = (
                spec.job_key,
                spec.job_type,
                spec.business_date.isoformat(),
                spec.input_snapshot,
                spec.engine_version,
                spec.params_hash,
                spec.loop_kind,
                spec.max_attempts,
            )
            prior = conn.execute(
                "SELECT job_key,job_type,business_date,input_snapshot,engine_version,params_hash,loop_kind,max_attempts FROM runtime_jobs WHERE job_key=?",
                (spec.job_key,),
            ).fetchone()
            if prior is not None and tuple(prior) != values:
                raise RuntimeError("job identity conflict")
            conn.execute(
                "INSERT OR IGNORE INTO runtime_jobs(job_key,job_type,business_date,input_snapshot,engine_version,params_hash,loop_kind,status,max_attempts,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?)",
                values + (now, now),
            )
            for dependency in dependencies:
                if not conn.execute(
                    "SELECT 1 FROM runtime_jobs WHERE job_key=?", (dependency,)
                ).fetchone():
                    raise ValueError("dependency must be registered first")
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_dependencies VALUES (?,?)",
                    (spec.job_key, dependency),
                )
            conn.commit()
            return spec.job_key
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def acquire(
        self, owner: str, *, loop_kind: str, lease_seconds: int = 60
    ) -> JobLease | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("invalid lease request")
        now = self.clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT j.* FROM runtime_jobs j WHERE j.loop_kind=? AND j.attempt_count<j.max_attempts AND ((j.status IN ('PENDING','WAITING_RETRY')) OR (j.status='RUNNING' AND j.lease_expires_at<?)) AND NOT EXISTS (SELECT 1 FROM runtime_dependencies d JOIN runtime_jobs p ON p.job_key=d.depends_on_job_key WHERE d.job_key=j.job_key AND p.status<>'SUCCEEDED') ORDER BY j.business_date,j.created_at LIMIT 1",
                (loop_kind, now.isoformat()),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["status"] == "RUNNING":
                conn.execute(
                    "UPDATE runtime_attempts SET status='LEASE_LOST',finished_at=? WHERE job_key=? AND status='RUNNING'",
                    (now.isoformat(), row["job_key"]),
                )
            token = row["fencing_token"] + 1
            attempt = row["attempt_count"] + 1
            expires = now + timedelta(seconds=lease_seconds)
            conn.execute(
                "UPDATE runtime_jobs SET status='RUNNING',attempt_count=?,lease_owner=?,lease_expires_at=?,fencing_token=?,updated_at=? WHERE job_key=?",
                (
                    attempt,
                    owner,
                    expires.isoformat(),
                    token,
                    now.isoformat(),
                    row["job_key"],
                ),
            )
            conn.execute(
                "INSERT INTO runtime_attempts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"attempt-{uuid4().hex}",
                    row["job_key"],
                    attempt,
                    owner,
                    token,
                    now.isoformat(),
                    None,
                    "RUNNING",
                    None,
                ),
            )
            conn.commit()
            return JobLease(row["job_key"], owner, token, attempt, expires)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _owned_update(
        self, lease: JobLease, sql: str, params: tuple[object, ...]
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                sql,
                params
                + (
                    self.clock().isoformat(),
                    lease.job_key,
                    lease.owner,
                    lease.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingViolation("stale owner or fencing token")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(self, lease: JobLease, lease_seconds: int = 60) -> None:
        now = self.clock()
        expires = now + timedelta(seconds=lease_seconds)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE runtime_jobs SET lease_expires_at=?,updated_at=? WHERE job_key=? AND lease_owner=? AND fencing_token=? AND status='RUNNING' AND lease_expires_at>=?",
                (
                    expires.isoformat(),
                    now.isoformat(),
                    lease.job_key,
                    lease.owner,
                    lease.fencing_token,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise FencingViolation("expired lease, stale owner or fencing token")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def checkpoint(self, lease: JobLease, checkpoint: dict[str, object]) -> None:
        payload = json.dumps(
            checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = self.clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE runtime_jobs SET checkpoint_json=?,updated_at=? WHERE job_key=? AND lease_owner=? AND fencing_token=? AND status='RUNNING' AND lease_expires_at>=?",
                (
                    payload,
                    now.isoformat(),
                    lease.job_key,
                    lease.owner,
                    lease.fencing_token,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise FencingViolation("expired lease, stale owner or fencing token")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish(
        self, lease: JobLease, *, success: bool, error_code: str | None = None
    ) -> str:
        conn = self._connect()
        now = self.clock().isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempt_count,max_attempts FROM runtime_jobs WHERE job_key=? AND lease_owner=? AND fencing_token=? AND status='RUNNING' AND lease_expires_at>=?",
                (lease.job_key, lease.owner, lease.fencing_token, now),
            ).fetchone()
            if row is None:
                raise FencingViolation("stale owner or fencing token")
            status = (
                "SUCCEEDED"
                if success
                else (
                    "WAITING_RETRY"
                    if row["attempt_count"] < row["max_attempts"]
                    else "FAILED"
                )
            )
            conn.execute(
                "UPDATE runtime_jobs SET status=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE job_key=?",
                (status, now, lease.job_key),
            )
            conn.execute(
                "UPDATE runtime_attempts SET status=?,finished_at=?,error_code=? WHERE job_key=? AND fencing_token=?",
                (
                    "SUCCEEDED" if success else "FAILED",
                    now,
                    error_code,
                    lease.job_key,
                    lease.fencing_token,
                ),
            )
            conn.commit()
            return status
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def defer(self, lease: JobLease, *, status: str, detail: str) -> None:
        """Release a leased job into an explicit non-success terminal/wait state."""
        if status not in {"WAITING_DATA", "DEGRADED", "CANCELLED"}:
            raise ValueError("unsupported deferred status")
        now = self.clock().isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE runtime_jobs SET status=?,checkpoint_json=?,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE job_key=? AND lease_owner=? "
                "AND fencing_token=? AND status='RUNNING'",
                (
                    status,
                    json.dumps({"detail": detail}, ensure_ascii=False, sort_keys=True),
                    now,
                    lease.job_key,
                    lease.owner,
                    lease.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise FencingViolation("stale owner or fencing token")
            conn.execute(
                "UPDATE runtime_attempts SET status='FAILED',finished_at=?,error_code=? "
                "WHERE job_key=? AND fencing_token=? AND status='RUNNING'",
                (now, status, lease.job_key, lease.fencing_token),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_owner(self, owner: str, *, detail: str = "worker stop requested") -> int:
        """Release this process's leases so a restart can recover safely.

        The business handlers are idempotent, so an interrupted leased job is
        returned to the finite retry queue.  Completed business rows are never
        rolled back or adjusted here.
        """

        if not owner:
            raise ValueError("owner is required")
        now = self.clock().isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT job_key,attempt_count,max_attempts FROM runtime_jobs "
                "WHERE lease_owner=? AND status='RUNNING'",
                (owner,),
            ).fetchall()
            for row in rows:
                status = "WAITING_RETRY" if row["attempt_count"] < row["max_attempts"] else "FAILED"
                conn.execute(
                    "UPDATE runtime_jobs SET status=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "checkpoint_json=?,updated_at=? WHERE job_key=? AND lease_owner=?",
                    (
                        status,
                        json.dumps({"detail": detail}, ensure_ascii=False, sort_keys=True),
                        now,
                        row["job_key"],
                        owner,
                    ),
                )
                conn.execute(
                    "UPDATE runtime_attempts SET status='FAILED',finished_at=?,error_code=? "
                    "WHERE job_key=? AND owner=? AND status='RUNNING'",
                    (now, "STOP_REQUESTED", row["job_key"], owner),
                )
            conn.execute(
                "UPDATE worker_heartbeats SET status='STOPPED',detail=?,heartbeat_at=? "
                "WHERE process_id=?",
                (detail, now, _owner_pid(owner)),
            )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _owner_pid(owner: str) -> int:
    """Extract a PID from the launcher owner convention without trusting it."""

    marker = owner.rsplit("-", 1)[-1]
    if not marker.isdigit():
        return -1
    return int(marker)
