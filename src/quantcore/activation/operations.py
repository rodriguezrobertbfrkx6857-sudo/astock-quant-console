from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, date, datetime

from src.quantcore.activation.data_worker import sync_dataset
from src.quantcore.activation.vendor import RelayClient
from src.quantcore.activation.status import latest_complete_market_session
from src.quantcore.contracts.config import V2Config
from src.quantcore.research.runner import execute_ready
from src.quantcore.runtime.jobs import JobLease, JobSpec, RuntimeStore


ENGINE_VERSION = "integrated-v2.activation.v2"
PRICE_DAILY_DATASETS = ("daily", "adj_factor", "stk_limit")
DAILY_STAGES = (
    "data_quality",
    "universe",
    "pit",
    "factors",
    "operational_strategies",
    "portfolio_risk",
    "paper",
    "report",
    "dashboard_publish",
)


def _snapshot(cfg: V2Config) -> str:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = list(
            conn.execute(
                "SELECT dataset,manifest_hash FROM data_publications ORDER BY dataset"
            )
        )
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _heartbeat(
    cfg: V2Config, worker_id: str, worker_type: str, status: str, detail: str
) -> None:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO worker_heartbeats VALUES (?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
            "worker_type=excluded.worker_type,status=excluded.status,process_id=excluded.process_id,"
            "detail=excluded.detail,heartbeat_at=excluded.heartbeat_at",
            (
                worker_id,
                worker_type,
                status,
                os.getpid(),
                detail,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _component_status(
    cfg: V2Config, component: str, status: str, detail: str, evidence: str | None = None
) -> None:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
            "status=excluded.status,detail=excluded.detail,evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            (component, status, detail, evidence, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def register_daily(cfg: V2Config, business_date: date) -> tuple[str, ...]:
    store = RuntimeStore(cfg.paths.state / "control.sqlite")
    snapshot = _snapshot(cfg)
    roots: list[str] = []
    for dataset in PRICE_DAILY_DATASETS:
        roots.append(
            store.register(
                JobSpec(
                    f"data.sync.{dataset}",
                    business_date,
                    snapshot,
                    ENGINE_VERSION,
                    {"dataset": dataset},
                    "DAILY",
                    3,
                )
            )
        )
    # Optional/vendor-specific datasets must not hold the EOD price path hostage.
    # The later stage handlers inspect their own required publications and fail
    # closed.  This lets Event/realtime/fundamental outages remain isolated.
    prior = tuple(
        roots[list(PRICE_DAILY_DATASETS).index(dataset)] for dataset in PRICE_DAILY_DATASETS
    )
    all_jobs = list(roots)
    for stage in DAILY_STAGES:
        key = store.register(
            JobSpec(
                f"daily.{stage}",
                business_date,
                snapshot,
                ENGINE_VERSION,
                {"stage": stage},
                "DAILY",
                3,
            ),
            prior,
        )
        all_jobs.append(key)
        prior = (key,)
    event = store.register(
        JobSpec(
            "event.sync",
            business_date,
            snapshot,
            ENGINE_VERSION,
            {"api": "anns_d"},
            "DAILY",
            3,
        )
    )
    watch = store.register(
        JobSpec(
            "watch.refresh",
            business_date,
            snapshot,
            ENGINE_VERSION,
            {"mode": "vendor"},
            "DAILY",
            3,
        )
    )
    all_jobs.extend((event, watch))
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        current = conn.execute(
            "SELECT status FROM operational_status WHERE component='daily_loop'"
        ).fetchone()
    finally:
        conn.close()
    if current is None or str(current[0]) != "PASS":
        _component_status(
            cfg, "daily_loop", "PENDING", f"registered {len(all_jobs)} idempotent jobs"
        )
    return tuple(all_jobs)


def latest_market_session(cfg: V2Config) -> date | None:
    """Return the newest common complete EOD session, never ``today``.

    A missing publication is a data condition, not permission to relabel the
    wall-clock date as an EOD session.  The runtime resolves the current
    business date separately through ``current_business_date``.
    """

    return latest_complete_market_session(cfg)


def register_research(cfg: V2Config, business_date: date) -> tuple[str, ...]:
    store = RuntimeStore(cfg.paths.state / "control.sqlite")
    snapshot = _snapshot(cfg)
    assess = store.register(
        JobSpec(
            "research.readiness",
            business_date,
            snapshot,
            ENGINE_VERSION,
            {"known_only": True},
            "RESEARCH",
            2,
        )
    )
    execute = store.register(
        JobSpec(
            "research.execute_ready",
            business_date,
            snapshot,
            ENGINE_VERSION,
            {"known_only": True, "untouched": False},
            "RESEARCH",
            2,
        ),
        (assess,),
    )
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        verified = conn.execute(
            "SELECT 1 FROM research_runs r JOIN stage09_evidence e "
            "ON e.research_run_id=r.research_run_id "
            "JOIN stage10_runs s10 ON s10.research_run_id=r.research_run_id "
            "WHERE r.status='SUCCEEDED' AND r.execution_scope='STRATEGY' "
            "AND s10.status='PASS' AND s10.evidence_valid=1 LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    _component_status(
        cfg,
        "research_loop",
        "PASS" if verified else "PENDING",
        "verified non-CONTROL research evidence retained; KNOWN_ONLY queue registered"
        if verified
        else "KNOWN_ONLY queue registered",
    )
    return assess, execute


def _job_metadata(cfg: V2Config, lease: JobLease) -> tuple[str, date]:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT job_type,business_date FROM runtime_jobs WHERE job_key=?",
            (lease.job_key,),
        ).fetchone()
        if row is None:
            raise KeyError(lease.job_key)
        return str(row[0]), date.fromisoformat(str(row[1]))
    finally:
        conn.close()


def _assess_research(cfg: V2Config) -> int:
    from src.quantcore.research.dependencies import assess_queue

    assessments = assess_queue(cfg.paths.state / "control.sqlite")
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in assessments:
            fingerprint = json.dumps(
                {
                    "queue": item["queue_item_id"],
                    "status": item["status"],
                    "reasons": item["reasons"],
                    "manifests": item["manifest_hashes"],
                    "requested_start": item["requested_start"],
                    "requested_end": item["requested_end"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assessment_id = hashlib.sha256(fingerprint.encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO queue_readiness_assessments VALUES (?,?,?,?,?,?)",
                (
                    assessment_id,
                    item["queue_item_id"],
                    item["status"],
                    json.dumps(item["reasons"], ensure_ascii=False),
                    json.dumps(item["manifest_hashes"], ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            if item["previous_status"] in {"PLANNED", "BLOCKED_DATA"} and item["previous_status"] != item["status"]:
                conn.execute(
                    "UPDATE research_queue SET status=?,updated_at=? WHERE queue_item_id=?",
                    (item["status"], datetime.now(UTC).isoformat(), item["queue_item_id"]),
                )
        conn.commit()
    finally:
        conn.close()
    ready = sum(1 for item in assessments if item["status"] == "READY")
    blocked = sum(1 for item in assessments if item["status"] == "BLOCKED_DATA")
    _component_status(
        cfg,
        "research_readiness",
        "PASS" if ready else "WAITING_DATA" if blocked else "PASS",
        f"assessed={len(assessments)}; ready={ready}; blocked_data={blocked}",
    )
    return ready


def _etf3_date_ready(cfg: V2Config, business_date: date) -> tuple[bool, str]:
    """Check the bounded ETF3 branch without claiming full stock coverage."""

    wanted = business_date.strftime("%Y%m%d")
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = {
            str(row[0]): (str(row[1]), row[2], row[3])
            for row in conn.execute(
                "SELECT p.dataset,m.readiness,json_extract(m.manifest_json,'$.start_date'),"
                "json_extract(m.manifest_json,'$.end_date') FROM data_publications p "
                "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                "WHERE p.dataset IN ('research_etf3_prices','research_etf3_limits',"
                "'known_etf3_prices','known_etf3_limits','historical_etf_master')"
            )
        }
    finally:
        conn.close()
    if "historical_etf_master" not in rows:
        return False, "ETF3 missing publications=historical_etf_master"
    if rows["historical_etf_master"][0] != "REAL_READY":
        return False, "ETF3 historical_etf_master is not REAL_READY"
    bundles = (
        ("research_etf3_prices", "research_etf3_limits", "RESEARCH_ETF3"),
        ("known_etf3_prices", "known_etf3_limits", "KNOWN_ETF3"),
    )
    missing_messages: list[str] = []
    for prices, limits, label in bundles:
        price_row = rows.get(prices)
        limit_row = rows.get(limits)
        if price_row is None or limit_row is None:
            missing_messages.append(f"{label}:missing")
            continue
        if price_row[0] != "REAL_READY" or limit_row[0] != "REAL_READY":
            missing_messages.append(f"{label}:not_REAL_READY")
            continue
        price_start, price_end = str(price_row[1] or ""), str(price_row[2] or "")
        limit_start, limit_end = str(limit_row[1] or ""), str(limit_row[2] or "")
        if price_start <= wanted <= price_end and limit_start <= wanted <= limit_end:
            return True, f"bounded ETF3 canonical branch available for {wanted}; bundle={label}"
        missing_messages.append(f"{label}:date_outside_range")
    return False, f"ETF3 date is not covered by a complete REAL_READY bundle: {';'.join(missing_messages)}"


def _run_stage(cfg: V2Config, store: RuntimeStore, lease: JobLease, component: str, ready: bool, detail: str) -> None:
    _component_status(
        cfg,
        component,
        "PASS" if ready else "BLOCKED_DATA",
        _bound_detail(cfg, lease, detail),
    )
    if ready:
        store.finish(lease, success=True)
    else:
        store.defer(
            lease,
            status="WAITING_DATA",
            detail=_bound_detail(cfg, lease, detail),
        )


def _bound_detail(cfg: V2Config, lease: JobLease, detail: str) -> str:
    """Attach the immutable date/snapshot/run binding to stage status text."""

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT business_date,input_snapshot,loop_kind FROM runtime_jobs WHERE job_key=?",
            (lease.job_key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return detail
    business_date, snapshot, loop_kind = str(row[0]), str(row[1]), str(row[2])
    run_id = f"{loop_kind.lower()}-{business_date}-{snapshot[:16]}"
    return (
        f"business_date={business_date};scope=LOCAL_{loop_kind}_PIPELINE;"
        f"snapshot={snapshot};run_id={run_id};detail={detail}"
    )


def _daily_data_ready(cfg: V2Config, business_date: date) -> tuple[bool, str]:
    """Validate persisted, current-session price publications for EOD."""
    wanted = business_date.strftime("%Y%m%d")
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT p.dataset,m.readiness,json_extract(m.manifest_json,'$.end_date') "
                "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                f"WHERE p.dataset IN ({','.join('?' for _ in PRICE_DAILY_DATASETS)})",
                PRICE_DAILY_DATASETS,
            )
        }
    finally:
        conn.close()
    missing = sorted(set(PRICE_DAILY_DATASETS) - set(rows))
    stale = sorted(
        dataset
        for dataset, (readiness, end_date) in rows.items()
        if readiness != "REAL_READY" or end_date != wanted
    )
    if missing or stale:
        return False, f"missing={','.join(missing) or '-'}; stale={','.join(stale) or '-'}"
    return True, f"REAL_READY session={wanted}; datasets={','.join(PRICE_DAILY_DATASETS)}"


def _published_for_session(
    cfg: V2Config, dataset: str, business_date: date
) -> bool:
    """Return true only for a verified publication covering this exact session."""
    wanted = business_date.strftime("%Y%m%d")
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT json_extract(m.manifest_json,'$.start_date'),"
            "json_extract(m.manifest_json,'$.end_date') FROM data_publications p "
            "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE p.dataset=? AND m.readiness='REAL_READY'",
            (dataset,),
        ).fetchone()
        if row is None:
            return False
        # Snapshot-style datasets are reusable when they are published for the
        # requested session.  Range data must end exactly on the requested
        # session so a later row can never leak into an earlier EOD run.
        if dataset in {"trade_cal", "stock_basic", "namechange", "fund_basic"}:
            return True
        start, end = str(row[0] or ""), str(row[1] or "")
        return bool(start and end and start <= wanted <= end and end == wanted)
    finally:
        conn.close()


def execute_one(cfg: V2Config, loop_kind: str, owner: str) -> bool:
    store = RuntimeStore(cfg.paths.state / "control.sqlite")
    lease = store.acquire(owner, loop_kind=loop_kind, lease_seconds=120)
    if lease is None:
        return False
    job_type, business_date = _job_metadata(cfg, lease)
    _heartbeat(
        cfg,
        owner,
        "RESEARCH" if loop_kind == "RESEARCH" else "DAILY",
        "RUNNING",
        job_type,
    )
    try:
        if job_type.startswith("data.sync."):
            dataset = job_type.removeprefix("data.sync.")
            if _published_for_session(cfg, dataset, business_date):
                store.finish(lease, success=True)
            else:
                result = sync_dataset(
                    cfg, dataset, business_date=business_date.isoformat()
                )
                if result["status"] == "REAL_READY":
                    store.finish(lease, success=True)
                else:
                    store.defer(
                        lease,
                        status="WAITING_DATA",
                        detail=_bound_detail(cfg, lease, str(result["status"])),
                    )
        elif job_type == "event.sync":
            result = sync_dataset(cfg, "anns_d", business_date=business_date.isoformat())
            if result["status"] == "REAL_READY":
                store.finish(lease, success=True)
            else:
                store.defer(
                    lease,
                    status="WAITING_DATA",
                    detail=_bound_detail(cfg, lease, str(result["status"])),
                )
        elif job_type == "watch.refresh":
            if RelayClient.configured():
                store.defer(
                    lease,
                    status="DEGRADED",
                    detail=_bound_detail(
                        cfg, lease, "realtime capability not yet vendor-verified"
                    ),
                )
            else:
                store.defer(
                    lease,
                    status="DEGRADED",
                    detail=_bound_detail(
                        cfg, lease, "PENDING_VENDOR: TUSHARE_RELAY_KEY absent"
                    ),
                )
            _component_status(
                cfg,
                "intraday_live",
                "PENDING_VENDOR",
                _bound_detail(cfg, lease, "no verified realtime vendor feed"),
            )
        elif job_type == "research.readiness":
            _assess_research(cfg)
            store.finish(lease, success=True)
        elif job_type == "research.execute_ready":
            result = execute_ready(cfg)
            if result.get("status") in {"SUCCEEDED", "ALREADY_SUCCEEDED"}:
                store.finish(lease, success=True)
            elif _assess_research(cfg) == 0:
                store.defer(
                    lease,
                    status="WAITING_DATA",
                    detail=_bound_detail(cfg, lease, "no READY queue items"),
                )
            else:
                store.defer(
                    lease,
                    status="DEGRADED",
                    detail=_bound_detail(
                        cfg,
                        lease,
                        str(
                            result.get(
                                "reason",
                                "real research executor has no implementation for a READY item",
                            )
                        ),
                    ),
                )
        elif job_type == "daily.data_quality":
            ready, detail = _daily_data_ready(cfg, business_date)
            _component_status(
                cfg,
                "daily_data_quality",
                "PASS" if ready else "BLOCKED_DATA",
                _bound_detail(cfg, lease, detail),
            )
            if ready:
                store.finish(lease, success=True)
            else:
                store.defer(
                    lease,
                    status="WAITING_DATA",
                    detail=_bound_detail(cfg, lease, detail),
                )
        elif job_type == "daily.universe":
            # Historical Security Master is mandatory for tradability.  A price
            # bar is never used as evidence of listing/ST/suspension state.
            conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
            try:
                state_count = int(
                    conn.execute(
                        "SELECT count(*) FROM security_state_versions "
                        "JOIN security_identities i ON i.asset_id=security_state_versions.asset_id "
                        "WHERE i.security_type='STOCK' AND effective_from<=? "
                        "AND (effective_to IS NULL OR effective_to>?)",
                        (business_date.isoformat(), business_date.isoformat()),
                    ).fetchone()[0]
                )
            finally:
                conn.close()
            etf_ready, etf_detail = _etf3_date_ready(cfg, business_date)
            if state_count or etf_ready:
                _run_stage(
                    cfg,
                    store,
                    lease,
                    "historical_universe",
                    True,
                    f"stock_state_rows={state_count}; {etf_detail if etf_ready else 'stock universe branch'}",
                )
            else:
                detail = f"no historical stock/ETF3 universe as-of {business_date.isoformat()}; {etf_detail}"
                _run_stage(cfg, store, lease, "historical_universe", False, detail)
        elif job_type == "daily.pit":
            conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
            try:
                version_count = int(
                    conn.execute("SELECT count(*) FROM security_state_versions").fetchone()[0]
                )
            finally:
                conn.close()
            _run_stage(
                cfg,
                store,
                lease,
                "daily_pit",
                version_count > 0,
                f"historical_state_versions={version_count}; all consumers retain mandatory as-of constraints",
            )
        elif job_type == "daily.factors":
            conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
            try:
                factor_count = int(conn.execute("SELECT count(*) FROM factor_versions").fetchone()[0])
                eval_count = int(conn.execute("SELECT count(*) FROM factor_evaluations").fetchone()[0])
            finally:
                conn.close()
            ready = factor_count > 0 and eval_count > 0
            detail = (
                f"factor_versions={factor_count}; evaluated={eval_count}; "
                "frozen research manifests are independent of PAPER execution freshness"
            )
            _run_stage(cfg, store, lease, "daily_factors", ready, detail)
        elif job_type == "daily.operational_strategies":
            conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
            try:
                run_count = int(conn.execute("SELECT count(*) FROM research_runs WHERE execution_scope='STRATEGY' AND status='SUCCEEDED'").fetchone()[0])
                stage10_count = int(conn.execute("SELECT count(*) FROM stage10_runs s JOIN research_runs r ON r.research_run_id=s.research_run_id WHERE r.execution_scope='STRATEGY' AND s.status='PASS'").fetchone()[0])
            finally:
                conn.close()
            _run_stage(
                cfg,
                store,
                lease,
                "operational_strategies",
                run_count > 0 and stage10_count > 0,
                f"strategy_runs={run_count}; stage10_pass={stage10_count}; no broker route",
            )
        elif job_type == "daily.portfolio_risk":
            from src.quantcore.rules.rulebook import default_rulebook

            rules = default_rulebook()
            _run_stage(
                cfg,
                store,
                lease,
                "portfolio_risk",
                bool(rules),
                f"frozen_rulebook={type(rules).__name__}; real_order_enabled=False",
            )
        elif job_type == "daily.paper":
            from src.quantcore.portfolio.paper_operations import (
                CURRENT_PAPER_PROFILE,
                operate,
                v2_accounts,
            )

            accounts = [
                item
                for item in v2_accounts(cfg)
                if item.get("data_profile") == CURRENT_PAPER_PROFILE
                and item.get("status") == "ACTIVE"
            ]
            # A current Daily run may only execute a current-forward generation
            # on its own bound business date.  Historical ETF3_CONTINUOUS
            # generations remain visible/audit-only; they are never replayed
            # by today's Daily loop.
            paper_date = business_date
            step_results: list[dict[str, object]] = []
            if paper_date is not None:
                for account in accounts:
                    strategy_id = str(account["strategy_id"])
                    request_hash = hashlib.sha256(
                        f"daily-paper|{strategy_id}|{paper_date.isoformat()}".encode()
                    ).hexdigest()
                    step_results.append(
                        operate(
                            cfg,
                            strategy_id,
                            "step",
                            trade_date=paper_date.strftime("%Y%m%d"),
                            request_hash=request_hash,
                        )
                    )
            step_states = [str(item.get("status")) for item in step_results]
            # WAITING_DATA is a fail-closed PAPER outcome (no fill and no
            # ledger mutation), but it is not an operational failure for the
            # independent Daily loop.  This is expected before a forward
            # generation's first session or while a genuine execution input
            # remains pending.
            paper_ready = not accounts or (
                paper_date is not None
                and all(state in {"SUCCEEDED", "WAITING_DATA"} for state in step_states)
            )
            _run_stage(
                cfg,
                store,
                lease,
                "paper_operations",
                paper_ready,
                f"qualified_accounts={len(accounts)}; market_date={business_date.isoformat()}; "
                f"paper_execution_date={paper_date.isoformat() if paper_date else 'PENDING'}; "
                f"freshness={'CURRENT' if paper_date == business_date else 'STALE_PENDING_VENDOR'}; "
                f"daily_steps={step_states or ['NO_ACCOUNT']}; real_orders=False",
            )
        elif job_type == "daily.report":
            conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
            try:
                publication_count = int(conn.execute("SELECT count(*) FROM reporting_publications WHERE provenance='REAL'").fetchone()[0])
            finally:
                conn.close()
            _run_stage(
                cfg,
                store,
                lease,
                "daily_report",
                publication_count > 0,
                f"real_reporting_publications={publication_count}",
            )
        elif job_type == "daily.dashboard_publish":
            dashboard = cfg.source_path.parents[2] / "dashboard" / "dist" / "index.html"
            _run_stage(
                cfg,
                store,
                lease,
                "dashboard_publish",
                dashboard.is_file(),
                f"dashboard_dist={dashboard}",
            )
            if dashboard.is_file():
                _component_status(
                    cfg,
                    "daily_loop",
                    "PASS",
                    _bound_detail(
                        cfg,
                        lease,
                        f"market EOD pipeline published for {business_date.isoformat()}; "
                        "PAPER freshness is independently reported; "
                        f"completed_at={datetime.now(UTC).isoformat()}",
                    ),
                )
        else:
            store.finish(lease, success=False, error_code="NOT_IMPLEMENTED")
            _component_status(
                cfg,
                job_type,
                "FAILED",
                _bound_detail(cfg, lease, "no handler is registered for this job type"),
            )
    except Exception as exc:
        store.finish(lease, success=False, error_code=type(exc).__name__)
        _heartbeat(
            cfg,
            owner,
            "RESEARCH" if loop_kind == "RESEARCH" else "DAILY",
            "FAILED",
            type(exc).__name__,
        )
        raise
    _heartbeat(
        cfg, owner, "RESEARCH" if loop_kind == "RESEARCH" else "DAILY", "IDLE", job_type
    )
    return True


def run_until_idle(
    cfg: V2Config, loop_kind: str, owner: str, *, max_jobs: int = 100
) -> int:
    count = 0
    while count < max_jobs and execute_one(cfg, loop_kind, owner):
        count += 1
    _heartbeat(
        cfg,
        owner,
        "RESEARCH" if loop_kind == "RESEARCH" else "DAILY",
        "IDLE",
        f"processed={count}",
    )
    return count
