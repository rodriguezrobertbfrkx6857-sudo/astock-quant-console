from __future__ import annotations

import json
import os
import sqlite3
import csv
import io
from hashlib import sha256
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.quantcore.contracts.config import load_v2_config
from src.quantcore.activation.status import current_business_date, daily_status
from src.quantcore.compat.local_archive import (
    LEGACY_PAPER,
    LEGACY_PROVENANCE,
    LocalArchiveAdapter,
)
from src.quantcore.data.manifest import ManifestStore
from src.quantcore.activation.vendor import RelayClient
from src.quantcore.intelligence.ai import (
    AIConfig,
    AIService,
    deepseek_api_key_present,
)
from src.quantcore.operations.profile import (
    load_operational_profile,
    validate_operational_profile,
)
from src.quantcore.research.dependencies import dependency_set
from src.quantcore.short_term import ShortTermWorkbench


CURRENT_STRATEGIES = {
    "etf_rotation.v1",
    "etf_lowvol.v1",
    "etf_trend_defensive.v1",
    "etf_momentum_20.v1",
    "etf_risk_adjusted_60.v1",
}
CURRENT_STRATEGY_VERSIONS = {
    "etf_rotation.v2": "etf_rotation.v1",
    "etf_lowvol.v1": "etf_lowvol.v1",
    "etf_trend_defensive.v1": "etf_trend_defensive.v1",
    "etf_momentum_20.v1": "etf_momentum_20.v1",
    "etf_risk_adjusted_60.v1": "etf_risk_adjusted_60.v1",
}
FROZEN_BACKTEST_RANGE = ("20190626", "20241231")
BLOCKED_FACTOR_DEPENDENCIES = {
    "TURNOVER_RATE": "reliable daily_basic turnover history is required",
    "BETA": "a validated HS300 historical benchmark profile is required",
    "IDIOSYNCRATIC_VOLATILITY": "a validated HS300 historical benchmark profile is required",
}

router = APIRouter(prefix="/api/v2")


def _ai_runtime() -> tuple[AIConfig, str]:
    config = AIConfig.from_environment()
    key_present = deepseek_api_key_present()
    if config.provider != "deepseek" or (key_present and not config.enabled):
        status = "DISABLED"
    elif not key_present:
        status = "PENDING_KEY"
    else:
        status = "READY"
    return config, status


class BacktestRunRequest(BaseModel):
    strategy_id: str = Field(default="etf_rotation.v1", min_length=1, max_length=100)
    profile_id: str = Field(default="ETF3_BOUNDED_V1", min_length=1, max_length=100)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    start_date: str = Field(default="", max_length=8)
    end_date: str = Field(default="", max_length=8)
    initial_cash: float = Field(default=1_000_000.0, ge=10_000.0)


class ResearchActionRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    queue_item_id: str | None = Field(default=None, max_length=128)


class EvaluationActionRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    strategy_id: str | None = Field(default=None, max_length=128)
    factor_id: str | None = Field(default=None, max_length=128)
    start_date: str = Field(default="", max_length=8)
    end_date: str = Field(default="", max_length=8)


class ManualHoldingRequest(BaseModel):
    asset_id: str = Field(..., min_length=9, max_length=9)
    shares: int = Field(..., ge=0)
    sellable_shares: int = Field(..., ge=0)
    today_bought_shares: int = Field(default=0, ge=0)
    cost_price: float = Field(..., gt=0)
    cash_budget: float | None = Field(default=None, ge=0)
    risk_profile: str = Field(default="BALANCED", pattern="^(CONSERVATIVE|BALANCED|AGGRESSIVE)$")
    as_of: str = Field(default="", max_length=64)
    active: bool = True


class HoldingCsvRequest(BaseModel):
    csv_text: str = Field(..., min_length=1, max_length=200_000)
    preview_only: bool = True


class ShortTermRunRequest(BaseModel):
    target_session: str = Field(default="", max_length=8)


def _backtest_range_reason(start_date: str, end_date: str) -> str | None:
    requested = (start_date, end_date)
    if requested == FROZEN_BACKTEST_RANGE:
        return None
    return (
        "requested backtest range does not match the frozen research protocol: "
        f"requested={start_date}..{end_date}, "
        f"required={FROZEN_BACKTEST_RANGE[0]}..{FROZEN_BACKTEST_RANGE[1]}; "
        "the request is blocked instead of silently changing the Stage09 evaluation window"
    )


class QueueActionRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    action: str = Field(..., pattern="^(run|retry|cancel|refresh)$")


class PaperActionRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    action: str = Field(..., pattern="^(admit|create_generation|step|step_span|pause|resume|reconcile)$")
    strategy_id: str | None = Field(default=None, max_length=128)
    trade_date: str = Field(default="", max_length=8)
    start_date: str = Field(default="", max_length=8)
    end_date: str = Field(default="", max_length=8)


class AIChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    ts_code: str | None = Field(default=None, max_length=20)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _meta() -> dict[str, Any]:
    cfg = load_v2_config()
    resolved_business_date = current_business_date(cfg)
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT p.* FROM current_reporting_publication c JOIN reporting_publications p ON p.publication_id=c.publication_id WHERE c.singleton=1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "schema_version": 2,
            "publication_id": "NO_PUBLICATION",
            "as_of": datetime.now(UTC).isoformat(),
            "provenance": "UNKNOWN",
            "quality_status": "FAIL",
            "stale": True,
            "evidence_refs": [],
            "state": "NO_CANONICAL_PUBLICATION",
            "business_date": resolved_business_date.isoformat() if resolved_business_date else None,
            "current_business_date": resolved_business_date.isoformat() if resolved_business_date else None,
            "error": "No verified canonical publication exists yet.",
        }
    historical_replay = str(row["run_id"]).startswith("real-etf3-replay-")
    historical = {
        "publication_id": row["publication_id"],
        "business_date": row["business_date"],
        "as_of": row["as_of"],
        "state": "HISTORICAL_REPLAY_PUBLICATION" if historical_replay else "CURRENT",
    }
    current_text = resolved_business_date.isoformat() if resolved_business_date else row["business_date"]
    return {
        "schema_version": 2,
        "publication_id": row["publication_id"],
        "as_of": datetime.now(UTC).isoformat(),
        "provenance": row["provenance"],
        "quality_status": row["quality_status"],
        "stale": historical_replay,
        "state": "CURRENT_DAILY_OPERATION_WITH_HISTORICAL_REPLAY" if historical_replay else "CURRENT",
        "business_date": current_text,
        "current_business_date": current_text,
        "historical_replay": historical,
        "evidence_refs": [f"run:{row['run_id']}", f"input:{row['input_hash']}"],
    }


def _response(**content: Any) -> dict[str, Any]:
    return {**_meta(), **content}


def _legacy() -> LocalArchiveAdapter:
    cfg = load_v2_config()
    return LocalArchiveAdapter(cfg.paths.legacy_archive)


def _legacy_or(default: Any, operation):
    try:
        return operation()
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
        return default


def _action_target(kind: str, idempotency_key: str) -> Path:
    cfg = load_v2_config(create_dirs=True)
    safe = sha256(idempotency_key.encode("utf-8")).hexdigest()
    return cfg.paths.artifacts / "evidence" / "PRODUCT_ACTIONS" / f"{kind}-{safe}.json"


def _read_action(kind: str, idempotency_key: str) -> dict[str, Any] | None:
    target = _action_target(kind, idempotency_key)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_action(
    kind: str, idempotency_key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    target = _action_target(kind, idempotency_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        existing = _read_action(kind, idempotency_key)
        if existing is not None:
            return existing
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return payload


def _action_response(
    kind: str,
    idempotency_key: str,
    *,
    status: str,
    request_hash: str | None = None,
    **content: Any,
) -> dict[str, Any]:
    existing = _read_action(kind, idempotency_key)
    if existing is not None:
        previous_hash = (existing.get("audit_record") or {}).get("request_hash")
        if request_hash and previous_hash and previous_hash != request_hash:
            return _response(
                action=kind,
                idempotency_key=idempotency_key,
                status="IDEMPOTENCY_CONFLICT",
                reason="The idempotency key is already bound to a different normalized request.",
                request_hash=request_hash,
            )
        return existing
    payload = _response(
        action=kind,
        idempotency_key=idempotency_key,
        status=status,
        audit_record={
            "action": kind,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(UTC).isoformat(),
            "legacy_write_attempted": False,
            "request_hash": request_hash,
        },
        request_hash=request_hash,
        **content,
    )
    return _write_action(kind, idempotency_key, payload)


def _cached_action(
    kind: str, idempotency_key: str, request_hash: str
) -> dict[str, Any] | None:
    """Return a previously committed action before executing side effects.

    The original response writer was idempotent only at persistence time: a
    repeated request could still run the business operation before the old
    response was read.  Action keys are now checked at the route boundary so
    a retry cannot duplicate a PAPER step or mutate state under a conflicting
    request body.
    """

    existing = _read_action(kind, idempotency_key)
    if existing is None:
        return None
    previous_hash = (existing.get("audit_record") or {}).get("request_hash")
    if previous_hash and previous_hash != request_hash:
        return _response(
            action=kind,
            idempotency_key=idempotency_key,
            status="IDEMPOTENCY_CONFLICT",
            reason="The idempotency key is already bound to a different normalized request.",
            request_hash=request_hash,
        )
    return existing


def _read_evidence(cfg, relative: str) -> dict[str, Any] | None:
    candidates = (
        cfg.paths.artifacts / "evidence" / relative,
        cfg.paths.artifacts / relative,
        cfg.paths.artifacts.parent / relative,
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _read_run_evidence(cfg, research_run_id: str) -> dict[str, Any] | None:
    """Resolve a persisted V2 run to its immutable evidence file."""

    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT evidence_ref,execution_scope FROM research_runs WHERE research_run_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (research_run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if str(row["execution_scope"]) == "STRATEGY" and "S13" in str(row["evidence_ref"]):
        return None
    evidence = _read_evidence(cfg, str(row["evidence_ref"]))
    if evidence is None or str(evidence.get("research_run_id")) != research_run_id:
        return None
    return evidence


def _request_hash(kind: str, payload: dict[str, Any]) -> str:
    """Hash the normalized action request, binding idempotency to its body."""

    return sha256(
        json.dumps(
            {"action": kind, "request": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _apply_factor_lifecycle(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep prior factor versions visible while disabling them as actions."""

    latest_version: dict[str, int] = {}
    for item in output:
        version_text = str(item.get("version") or "v0").lower().removeprefix("v")
        version_number = int(version_text) if version_text.isdigit() else 0
        base_factor_id = str(item.get("base_factor_id") or "")
        latest_version[base_factor_id] = max(
            latest_version.get(base_factor_id, 0), version_number
        )
    for item in output:
        version_text = str(item.get("version") or "v0").lower().removeprefix("v")
        version_number = int(version_text) if version_text.isdigit() else 0
        base_factor_id = str(item.get("base_factor_id") or "")
        superseded = version_number < latest_version.get(base_factor_id, version_number)
        item["lifecycle"] = "SUPERSEDED_AUDIT_ONLY" if superseded else "CURRENT"
        item["executable"] = not superseded and bool(item.get("dependency_ready", True))
        item["superseded"] = superseded
    return output


def _v2_factor_items(cfg, limit: int = 200) -> list[dict[str, Any]]:
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT f.*,e.evaluation_id,e.snapshot_hash AS evaluation_snapshot_hash,"
            "e.status AS evaluation_status,e.metrics_json,e.evidence_ref,e.created_at AS evaluated_at "
            "FROM factor_versions f LEFT JOIN factor_evaluations e ON e.evaluation_id=("
            "SELECT x.evaluation_id FROM factor_evaluations x WHERE x.factor_version_id=f.factor_version_id "
            "ORDER BY x.created_at DESC LIMIT 1) ORDER BY f.factor_version_id LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    output: list[dict[str, Any]] = []
    for row in rows:
        evaluation = None
        metrics: dict[str, Any] = {}
        if row["evaluation_id"]:
            try:
                metrics = json.loads(str(row["metrics_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = {}
            evaluation = {
                "evaluation_id": row["evaluation_id"],
                "status": row["evaluation_status"],
                "snapshot_hash": row["evaluation_snapshot_hash"],
                "metrics": metrics,
                "evidence_ref": row["evidence_ref"],
                "evaluated_at": row["evaluated_at"],
            }
        definition = json.loads(str(row["definition_json"]))
        dependency_reason = BLOCKED_FACTOR_DEPENDENCIES.get(str(row["family_id"]))
        status = "BLOCKED_DATA" if dependency_reason else str(row["evaluation_status"] or row["status"])
        output.append(
            {
                "factor_id": row["factor_version_id"],
                "factor_version_id": row["factor_version_id"],
                "base_factor_id": row["factor_id"],
                "name": f"{row['factor_id']} {row['version']}",
                "category": row["family_id"],
                "version": row["version"],
                "status": status,
                "research_status": status,
                "dependency_ready": dependency_reason is None,
                "accepted": status == "COMPLETED" and int(metrics.get("cross_section_count", 0)) >= 5,
                "paper_eligible": False,
                "provenance": "V2_CANONICAL",
                "source_kind": "V2_CANONICAL",
                "canonical_v2": True,
                "v2_revalidated": True,
                "definition": definition,
                "params": definition,
                "source_code_hash": row["source_code_hash"],
                "parameter_hash": row["parameter_hash"],
                "universe_version": row["universe_version"],
                "stats": {
                    "ic_mean": metrics.get("ic"),
                    "rank_ic_mean": metrics.get("rank_ic"),
                    "coverage": metrics.get("coverage"),
                    "coverage_display": (
                        f"{float(metrics['coverage']) * 100:.1f}%"
                        if metrics.get("coverage") is not None
                        else None
                    ),
                    "missing_ratio": metrics.get("missing_ratio"),
                    "turnover": metrics.get("top_quintile_turnover"),
                    "quantile_returns": metrics.get("quantile_returns", {}),
                },
                "coverage": {
                    "rows": metrics.get("factor_observations", 0),
                    "start": definition.get("evaluation_start"),
                    "end": definition.get("evaluation_end"),
                    "assets": metrics.get("assets", []),
                },
                "evaluation": evaluation,
                "block_reasons": (
                    [dependency_reason]
                    if dependency_reason
                    else metrics.get("evaluation_reasons", [])
                ),
            }
        )
    return _apply_factor_lifecycle(output)


def _parse_json(value: object, fallback: object) -> object:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def _json_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _v2_strategy_rows(cfg) -> list[dict[str, Any]]:
    """Build strategy registry rows from V2 state, including real gates."""

    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT sv.strategy_version_id,sv.family_id,sv.protocol_hash,sv.source_code_hash,"
            "sv.snapshot_hash,sv.universe_version,sv.created_at,p.protocol_id,p.protocol_json,"
            "q.queue_item_id,q.priority,q.status AS queue_status,q.attempt,q.updated_at "
            "FROM strategy_versions sv JOIN research_protocols p ON p.protocol_hash=sv.protocol_hash "
            "LEFT JOIN research_queue q ON q.strategy_version_id=sv.strategy_version_id "
            "AND q.rowid=(SELECT max(q2.rowid) FROM research_queue q2 WHERE q2.strategy_version_id=sv.strategy_version_id) "
            "ORDER BY coalesce(q.priority,999),sv.strategy_version_id"
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            stage = conn.execute(
                "SELECT s.result_id,s.rating,s.reasons_json,s.primary_metric,s.effective_trades,"
                "s.validation_blocks,s.snapshot_hash,s.created_at,e.research_run_id,e.metrics_json "
                "FROM stage09_results s LEFT JOIN stage09_evidence e ON e.result_id=s.result_id "
                "WHERE s.queue_item_id=? "
                "ORDER BY CASE WHEN s.rating='INSUFFICIENT_DATA' THEN 1 ELSE 0 END, "
                "s.validation_blocks DESC,s.created_at DESC LIMIT 1",
                (row["queue_item_id"],),
            ).fetchone() if row["queue_item_id"] else None
            if stage and stage["research_run_id"]:
                run = conn.execute(
                    "SELECT research_run_id,execution_scope,status,evidence_ref,metrics_json,created_at "
                    "FROM research_runs WHERE research_run_id=? LIMIT 1",
                    (stage["research_run_id"],),
                ).fetchone()
            else:
                run = conn.execute(
                    "SELECT research_run_id,execution_scope,status,evidence_ref,metrics_json,created_at "
                    "FROM research_runs WHERE strategy_version_id=? ORDER BY created_at DESC LIMIT 1",
                    (row["strategy_version_id"],),
                ).fetchone()
            stage10 = conn.execute(
                "SELECT result_id,status,evidence_valid,formal_hash,reference_hash,created_at "
                "FROM stage10_runs WHERE research_run_id=? ORDER BY created_at DESC LIMIT 1",
                (run["research_run_id"],),
            ).fetchone() if run else None
            qualification = conn.execute(
                "SELECT q.qualification_id,q.snapshot_hash,e.status,e.created_at "
                "FROM paper_qualifications q JOIN qualification_events e ON e.qualification_id=q.qualification_id "
                "WHERE q.strategy_version_id=? AND e.rowid=(SELECT max(x.rowid) FROM qualification_events x WHERE x.qualification_id=q.qualification_id) "
                "LIMIT 1",
                (row["strategy_version_id"],),
            ).fetchone()
            paper_account = conn.execute(
                "SELECT p.account_id,e.status FROM paper_accounts p JOIN paper_account_events e ON e.account_id=p.account_id "
                "WHERE p.strategy_version_id=? AND e.rowid=(SELECT max(x.rowid) FROM paper_account_events x WHERE x.account_id=p.account_id) LIMIT 1",
                (row["strategy_version_id"],),
            ).fetchone()
            current_generation = conn.execute(
                "SELECT g.account_id,e.status FROM paper_generations g JOIN paper_generation_events e ON e.generation_id=g.generation_id "
                "WHERE g.strategy_version_id=? AND g.data_profile='ETF3_FORWARD_CURRENT' "
                "AND e.rowid=(SELECT max(x.rowid) FROM paper_generation_events x WHERE x.generation_id=g.generation_id) "
                "AND e.status IN ('ACTIVE','PAUSED') ORDER BY g.created_at DESC LIMIT 1",
                (row["strategy_version_id"],),
            ).fetchone()
            reasons = _json_list(_parse_json(stage["reasons_json"], [])) if stage else []
            metrics = _json_dict(_parse_json(stage["metrics_json"], {})) if stage and stage["metrics_json"] else {}
            protocol = _json_dict(_parse_json(row["protocol_json"], {}))
            executable = row["strategy_version_id"] in CURRENT_STRATEGY_VERSIONS
            output.append(
                {
                    "name": row["family_id"],
                    "strategy_id": row["strategy_version_id"],
                    "strategy_version_id": row["strategy_version_id"],
                    "strategy_key": CURRENT_STRATEGY_VERSIONS.get(row["strategy_version_id"]),
                    "category": row["family_id"],
                    "family_id": row["family_id"],
                    "protocol_id": row["protocol_id"],
                    "protocol_hash": row["protocol_hash"],
                    "source_code_hash": row["source_code_hash"],
                    "snapshot_hash": row["snapshot_hash"],
                    "universe_version": row["universe_version"],
                    "queue_item_id": row["queue_item_id"],
                    "priority": row["priority"],
                    "registry_status": row["queue_status"] or "REGISTERED",
                    "research_status": stage["rating"] if stage else row["queue_status"] or "REGISTERED",
                    "rating": stage["rating"] if stage else row["queue_status"] or "REGISTERED",
                    "reasons": reasons,
                    "block_reasons": reasons,
                    "last_evaluated": stage["created_at"] if stage else None,
                    "stage09": dict(stage) if stage else None,
                    "stage09_metrics": metrics,
                    "research_run": dict(run) if run else None,
                    "stage10": dict(stage10) if stage10 else None,
                    "qualification": dict(qualification) if qualification else None,
                    "paper_eligible": bool(qualification and qualification["status"] == "ACTIVE"),
                    "has_paper_account": bool(current_generation),
                    "paper_account_id": current_generation["account_id"] if current_generation else None,
                    "current_paper_status": current_generation["status"] if current_generation else "NOT_CREATED",
                    "has_historical_paper_account": bool(paper_account),
                    "historical_paper_account_id": paper_account["account_id"] if paper_account else None,
                    "executable": executable,
                    "control_only": bool(protocol.get("control_only", False)),
                    "protocol": protocol,
                    "evaluation": {
                        "verdict": stage["rating"] if stage else None,
                        "metrics": metrics,
                        "research_run_id": run["research_run_id"] if run else None,
                    } if stage or run else None,
                    "dependency_set": dependency_set(protocol if isinstance(protocol, dict) else {}),
                    "data_source": "V2_CANONICAL",
                    "source_kind": "V2_CANONICAL",
                    "provenance": "V2_CANONICAL",
                    "publication_state": "V2_STATE",
                    "v2_revalidated": executable and bool(run),
                }
            )
    finally:
        conn.close()
    return output


def _v2_run_detail(cfg, backtest_id: str) -> dict[str, Any] | None:
    prefix = "v2-run-"
    if not backtest_id.startswith(prefix):
        return None
    run_id = backtest_id.removeprefix(prefix)
    evidence = _read_run_evidence(cfg, run_id)
    if not evidence:
        return None
    metrics = _json_dict(evidence.get("metrics"))
    canonical = _json_dict(metrics.get("canonical"))
    actual = _json_dict(evidence.get("actual_range"))
    requested = _json_dict(evidence.get("requested_range"))
    windows = [item for item in _json_list(evidence.get("windows")) if isinstance(item, dict)]
    expected_start = windows[0].get("declared_start") if windows else None
    expected_end = windows[-1].get("declared_end") if windows else None
    protocol_range_match = bool(
        expected_start
        and expected_end
        and requested.get("start") == expected_start
        and requested.get("end") == expected_end
        and actual.get("start") == expected_start
        and actual.get("end") == expected_end
    )
    stage10_status = (evidence.get("stage10") or {}).get("status")
    snapshots = _json_list(evidence.get("daily_snapshots"))
    nav_series = [
        {
            "trade_date": item.get("trade_date") or item.get("on"),
            "nav": item.get("nav"),
            "cash": item.get("cash"),
            "positions": item.get("positions", []),
        }
        for item in snapshots
        if isinstance(item, dict)
    ]
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        for order in item.get("orders", []) if isinstance(item.get("orders"), list) else []:
            if isinstance(order, dict):
                orders.append({"trade_date": item.get("trade_date") or item.get("on"), **order})
        for fill in item.get("fills", []) if isinstance(item.get("fills"), list) else []:
            if isinstance(fill, dict):
                fills.append({"trade_date": item.get("trade_date") or item.get("on"), **fill})
    strategy_id = str(evidence.get("strategy_key") or "")
    return {
        "backtest_id": backtest_id,
        "bt_id": backtest_id,
        "research_run_id": run_id,
        "strategy_id": strategy_id,
        "strategy_name": strategy_id,
        "start": actual.get("start"),
        "end": actual.get("end"),
        "engine": "quantcore.formal",
        "engine_version": "V2",
        "provenance": "REAL",
        "legacy_status": "V2_STRATEGY_RUN" if protocol_range_match else "AUDIT_ONLY_PROTOCOL_MISMATCH",
        "v2_revalidated": protocol_range_match,
        "protocol_range_match": protocol_range_match,
        "protocol_range": {"start": expected_start, "end": expected_end},
        "stage09": evidence.get("stage09"),
        "stage09_status": (evidence.get("stage09") or {}).get("rating"),
        "stage10_status": stage10_status if protocol_range_match else "AUDIT_ONLY_PROTOCOL_MISMATCH",
        "stage10_engine_status": stage10_status,
        "stage10_result_id": (evidence.get("stage10") or {}).get("result_id"),
        "stage10_differences": (evidence.get("stage10") or {}).get("differences", 0),
        "formal_hash": (evidence.get("stage10") or {}).get("formal_hash"),
        "reference_hash": (evidence.get("stage10") or {}).get("reference_hash"),
        "fills": len(fills),
        "orders": len(orders),
        "final_nav": canonical.get("final_nav", nav_series[-1].get("nav") if nav_series else None),
        "total_return": canonical.get("total_return"),
        "nav_series": nav_series,
        "daily_snapshots": snapshots,
        "orders_detail": orders,
        "fills_detail": fills,
        "signals": evidence.get("signals", []),
        "metrics": metrics,
        "evidence_ref": evidence.get("evidence_ref") or f"run:{run_id}",
    }


def _v2_accounts(cfg) -> list[dict[str, Any]]:
    from src.quantcore.portfolio.paper_operations import v2_accounts

    return v2_accounts(cfg)


def _queue_row(cfg, queue_item_id: str) -> sqlite3.Row | None:
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        return conn.execute(
            "SELECT q.queue_item_id,q.strategy_version_id,q.status,q.attempt,p.protocol_id,p.family_id "
            "FROM research_queue q JOIN research_protocols p ON p.protocol_hash=q.protocol_hash "
            "WHERE q.queue_item_id=?",
            (queue_item_id,),
        ).fetchone()
    finally:
        conn.close()


def _is_executable_queue(row: sqlite3.Row | None) -> bool:
    return bool(
        row
        and (
            str(row["strategy_version_id"]) in CURRENT_STRATEGY_VERSIONS
            or str(row["strategy_version_id"]) == "CTRL.declaration.v1"
        )
    )


def _refresh_queue_state(cfg, queue_item_id: str | None = None) -> dict[str, Any]:
    """Recompute and persist current dependency evidence for V2 queue items."""

    from src.quantcore.research.dependencies import assess_queue

    assessments = assess_queue(cfg.paths.state / "control.sqlite")
    selected = [
        item
        for item in assessments
        if queue_item_id is None or item["queue_item_id"] == queue_item_id
    ]
    if queue_item_id and not selected:
        raise KeyError(queue_item_id)
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = 0
        for item in selected:
            reasons = _json_list(item.get("reasons", []))
            fingerprint = json.dumps(
                {
                    "queue": item["queue_item_id"],
                    "status": item["status"],
                    "reasons": reasons,
                    "manifests": item["manifest_hashes"],
                    "requested_start": item["requested_start"],
                    "requested_end": item["requested_end"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assessment_id = sha256(fingerprint.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO queue_readiness_assessments VALUES (?,?,?,?,?,?)",
                (
                    assessment_id,
                    item["queue_item_id"],
                    item["status"],
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(item["manifest_hashes"], ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            previous = str(item["previous_status"])
            if previous in {"PLANNED", "BLOCKED_DATA"} and previous != item["status"]:
                conn.execute(
                    "UPDATE research_queue SET status=?,updated_at=? WHERE queue_item_id=?",
                    (item["status"], datetime.now(UTC).isoformat(), item["queue_item_id"]),
                )
                changed += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"assessments": selected, "changed": changed}


def _profile_response(cfg) -> dict[str, Any]:
    profile = load_operational_profile(cfg)
    validation = validate_operational_profile(cfg)
    return {
        "profile_id": profile["profile_id"],
        "version": profile["version"],
        "scope": profile["scope"],
        "full_scope_status": profile["full_scope_status"],
        "supported_profile_status": profile["supported_profile_status"],
        "mode": profile["mode"],
        "supported_securities": profile["supported_securities"],
        "supported_date_windows": profile["supported_date_windows"],
        "required_manifests": validation.required_manifests,
        "execution_rules": profile["execution_rules"],
        "pit_limits": profile["pit_limits"],
        "research_scope": profile["research_scope"],
        "known_limitations": profile["known_limitations"],
        "validation": validation.as_dict(),
    }


def _runtime_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status,count(*) FROM runtime_jobs GROUP BY status ORDER BY status"
        ).fetchall()
    }


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _operation_scope(component: str) -> str:
    if component.startswith("data_history:") or component in {
        "historical_universe",
        "historical_20d_replay",
    }:
        return "HISTORICAL_AUDIT"
    if component in {"data:trade_cal", "scheduler"}:
        return "CURRENT_SHARED"
    if component == "startup_incremental_sync":
        # This component reports the optional full-market daily/adj_factor/
        # stk_limit refresh.  The independently published ETF3 current path
        # is represented by current_etf_scope and must not inherit a vendor
        # warning from the broader universe.
        return "FULL_MARKET_OPTIONAL"
    if component.startswith("data:etf3_") or component in {
        "current_etf_scope",
        "current_paper_scope",
        "supported_daily_loop",
        "intraday_live",
        "startup_incremental_sync",
        "daily_loop",
        "runtime_loop",
    }:
        return "CURRENT_ETF3"
    if component.startswith("research") or component.startswith("data:research_"):
        return "CURRENT_RESEARCH"
    if component.startswith("data:") or component == "daily_data_quality":
        return "FULL_MARKET_OPTIONAL"
    return "CURRENT_LOCAL"


def _status_semantics(status: str, *, component: str, scope: str, detail: str) -> dict[str, str]:
    normalized = status.upper()
    if normalized in {"PASS", "READY", "SUCCEEDED", "ACTIVE", "IDLE"}:
        severity = "INFO"
        reason = "CURRENT_EVIDENCE_PASS"
        impact = "No user action is required for this capability."
        next_action = "None."
    elif normalized in {"FAIL", "FAILED", "ERROR"}:
        severity = "CRITICAL"
        reason = "EXECUTION_OR_INTEGRITY_FAILURE"
        impact = "The bound capability cannot be relied on until a fresh successful check exists."
        next_action = "Inspect the evidence reference and retry the same bounded operation."
    elif "BLOCK" in normalized:
        severity = "WARN"
        reason = "REQUIRED_DATA_OR_POLICY_GATE"
        impact = "Only the named scope is blocked; unrelated current capabilities remain independently usable."
        next_action = "Resolve the named data/policy dependency without weakening the gate."
    elif any(token in normalized for token in ("WAIT", "PENDING", "DEGRADED")):
        severity = "INFO" if scope == "HISTORICAL_AUDIT" else "WARN"
        reason = "EXPECTED_WAIT_OR_EXTERNAL_DEPENDENCY"
        impact = "The named capability is waiting or degraded; this is not evidence of success."
        next_action = "Retry when its stated data, provider, or market-session condition is available."
    else:
        severity = "INFO"
        reason = "INFORMATIONAL_STATE"
        impact = "See the component detail for its scoped meaning."
        next_action = "No automatic action is inferred from the status text."
    if scope == "HISTORICAL_AUDIT":
        severity = "INFO"
        impact = "Retained audit history; it does not describe the current supported scope."
        next_action = "Use the history filter only when reviewing prior evidence."
    return {
        "severity": severity,
        "reason": reason,
        "impact": impact,
        "next_action": next_action,
        "detail": detail,
    }


def _enrich_operation(row: dict[str, Any], *, checked_at: datetime) -> dict[str, Any]:
    component = str(row.get("component") or "")
    status = str(row.get("status") or "UNKNOWN")
    detail = str(row.get("detail") or "")
    scope = _operation_scope(component)
    updated = _parse_utc_timestamp(row.get("updated_at"))
    age = max(0, int((checked_at - updated).total_seconds())) if updated else None
    freshness = "FRESH" if age is not None and age <= 600 else "STALE"
    return {
        **row,
        "scope": scope,
        "current": scope != "HISTORICAL_AUDIT",
        "checked_at": checked_at.isoformat(),
        "freshness": freshness,
        "age_seconds": age,
        "superseded": False,
        **_status_semantics(status, component=component, scope=scope, detail=detail),
    }


def _stack_health(cfg) -> dict[str, Any]:
    path = cfg.paths.artifacts / "evidence" / "FULL_ACTIVATION" / "stack_health.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = dict(value)
    checked_at = datetime.now(UTC)
    generated_at = _parse_utc_timestamp(result.get("generated_at"))
    age = max(0, int((checked_at - generated_at).total_seconds())) if generated_at else None
    fresh = age is not None and age <= 180
    result["checked_at"] = checked_at.isoformat()
    result["evidence_age_seconds"] = age
    result["fresh"] = fresh
    if not fresh:
        recorded = str(result.get("overall") or "UNKNOWN")
        result["recorded_overall"] = recorded
        result["overall"] = "UNKNOWN_STALE"
        result["one_click_start_stop"] = "UNKNOWN_STALE"
        result["detail"] = (
            f"health snapshot is stale (recorded={recorded}, age_seconds={age}); "
            "run the bounded stack health check before relying on it"
        )
    return result


def _current_etf_scope(cfg) -> dict[str, Any]:
    """Return bounded current ETF evidence independently of full-market state."""

    evidence = _read_evidence(cfg, "CURRENT_SCOPE/current_etf_scope.json")
    if evidence is not None:
        return evidence
    return {
        "status": "WAITING_DATA",
        "scope": "ETF3_CURRENT",
        "codes": ["510300.SH", "510500.SH", "159915.SZ"],
        "current_eod_ready": False,
        "current_paper_lookback_ready": False,
        "latest_complete_eod": None,
        "next_forward_session": None,
        "missing_complete_sessions": [],
        "reason": "CURRENT_SCOPE evidence has not been published yet.",
    }


def _publication_rows(dataset: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT m.manifest_id,m.manifest_json FROM data_publications p "
            "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE p.dataset=? AND m.readiness='REAL_READY' AND m.provenance='REAL'",
            (dataset,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}, []
    store = ManifestStore(
        cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256"
    )
    manifest = store.load(str(row["manifest_id"]))
    rows: list[dict[str, Any]] = []
    for digest in manifest.object_hashes:
        path = store.object_root / digest[:2] / digest
        rows.extend(json.loads(path.read_text("utf-8")))
    return json.loads(str(row["manifest_json"])), rows


def _product_catalog_summary(cfg) -> dict[str, Any]:
    archive_pointer = cfg.paths.legacy_archive / "current.json"
    if archive_pointer.is_file():
        try:
            pointer = json.loads(archive_pointer.read_text(encoding="utf-8"))
            return {
                "status": "PASS",
                "path": str(archive_pointer),
                "version_id": pointer.get("version_id"),
                "provenance": LEGACY_PROVENANCE,
                "canonical_v2": False,
                "legacy_write_attempted": False,
                "source_paths_loaded_by_runtime": False,
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    path = cfg.paths.state / "product_catalog.sqlite"
    if not path.exists():
        return {"status": "NOT_BUILT", "path": str(path)}
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            counts = {
                "catalog_publications": int(conn.execute("SELECT count(*) FROM catalog_publications").fetchone()[0]),
                "legacy_factor_registry": int(conn.execute("SELECT count(*) FROM legacy_factor_registry").fetchone()[0]),
                "legacy_strategy_registry": int(conn.execute("SELECT count(*) FROM legacy_strategy_registry").fetchone()[0]),
                "legacy_backtest_registry": int(conn.execute("SELECT count(*) FROM legacy_backtest_registry").fetchone()[0]),
                "v2_factor_registry": int(conn.execute("SELECT count(*) FROM v2_factor_registry").fetchone()[0]),
            }
        finally:
            conn.close()
        return {
            "status": "PASS",
            "path": str(path),
            "provenance": LEGACY_PROVENANCE,
            "canonical_v2": False,
            "legacy_write_attempted": False,
            **counts,
        }
    except sqlite3.Error as exc:
        return {"status": "ERROR", "path": str(path), "message": str(exc)}


def _product_catalog_datasets(cfg) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    archive_pointer = cfg.paths.legacy_archive / "current.json"
    if archive_pointer.is_file():
        try:
            pointer = json.loads(archive_pointer.read_text(encoding="utf-8"))
            catalog_path = (archive_pointer.parent / str(pointer["catalog"])).resolve(strict=True)
            if catalog_path.is_relative_to(cfg.paths.data):
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                if isinstance(catalog, dict):
                    catalog_datasets = catalog.get("datasets", [])
                    catalog_databases = catalog.get("databases", [])
                    if isinstance(catalog_datasets, list) and isinstance(catalog_databases, list):
                        return [dict(item) for item in catalog_datasets if isinstance(item, dict)], [dict(item) for item in catalog_databases if isinstance(item, dict)]
        except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
    path = cfg.paths.state / "product_catalog.sqlite"
    if not path.exists():
        return [], []
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            raw_rows = conn.execute(
                "SELECT dataset,source_database,source_table,metadata_json "
                "FROM catalog_publications ORDER BY dataset,source_database,source_table"
            ).fetchall()
        finally:
            conn.close()
        datasets: list[dict[str, Any]] = []
        databases: dict[str, dict[str, Any]] = {}
        for dataset, database, table, metadata_json in raw_rows:
            try:
                item = json.loads(str(metadata_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                item = {}
            if not isinstance(item, dict):
                item = {}
            item.update(
                {
                    "dataset": dataset,
                    "database": database,
                    "table": table,
                    "source_kind": LEGACY_PROVENANCE,
                    "provenance": LEGACY_PROVENANCE,
                    "publication_state": "LEGACY_EVIDENCE",
                    "row_count": item.get("row_count") or item.get("rows"),
                    "unique_codes": item.get("unique_codes") or item.get("symbols"),
                    "earliest_date": item.get("earliest_date") or item.get("start"),
                    "latest_date": item.get("latest_date") or item.get("end"),
                }
            )
            datasets.append(item)
            database_key = str(database or "")
            if database_key and database_key not in databases:
                databases[database_key] = {
                    "database": database_key,
                    "source_kind": LEGACY_PROVENANCE,
                    "publication_state": "LEGACY_EVIDENCE",
                }
        return datasets, list(databases.values())
    except sqlite3.Error:
        return [], []


@router.get("/profile")
def profile():
    cfg = load_v2_config()
    return _response(**_profile_response(cfg))


@router.get("/overview")
def overview():
    cfg = load_v2_config()
    daily_truth = daily_status(cfg)
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        queue_rows = conn.execute(
            "SELECT status,count(*) n FROM research_queue GROUP BY status ORDER BY status"
        ).fetchall()
        queue = {str(row[0]): int(row[1]) for row in queue_rows}
        runtime_counts = _runtime_counts(conn)
        current_runtime_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT status,count(*) FROM runtime_jobs WHERE "
                "(loop_kind='DAILY' AND business_date=? AND input_snapshot=?) OR "
                "(loop_kind='RESEARCH' AND business_date=?) GROUP BY status ORDER BY status",
                (
                    daily_truth["business_date"] or "",
                    daily_truth["snapshot"],
                    daily_truth["business_date"] or "",
                ),
            )
        }
        publications = [
            (str(row[0]), json.loads(str(row[1])))
            for row in conn.execute(
                "SELECT p.dataset,m.manifest_json FROM data_publications p "
                "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                "WHERE m.readiness='REAL_READY' AND m.provenance='REAL'"
            )
        ]
        curated_count, curated_rows = conn.execute(
            "SELECT count(*),coalesce(sum(json_extract(manifest_json,'$.row_count')),0) "
            "FROM data_manifests WHERE layer='CURATED' AND readiness='REAL_READY' AND provenance='REAL'"
        ).fetchone()
        research_runs = int(conn.execute("SELECT count(*) FROM research_runs").fetchone()[0])
        research_run_rows = conn.execute(
            "SELECT research_run_id,strategy_version_id,protocol_hash,snapshot_hash,execution_scope "
            "FROM research_runs"
        ).fetchall()
        research_identity_counts: dict[tuple[str, str, str], int] = {}
        recovery_run_count = 0
        for row in research_run_rows:
            identity = (str(row[1]), str(row[2]), str(row[3]))
            research_identity_counts[identity] = research_identity_counts.get(identity, 0) + 1
            if any(token in str(row[0]).lower() for token in ("recovery", "repeat", "duplicate")):
                recovery_run_count += 1
        duplicate_research_run_count = sum(
            max(0, count - 1) for count in research_identity_counts.values()
        )
        stage09_count = int(conn.execute("SELECT count(*) FROM stage09_results").fetchone()[0])
        stage10_rows = conn.execute(
            "SELECT result_id,research_run_id,status,evidence_valid,formal_hash,reference_hash,created_at "
            "FROM stage10_runs ORDER BY created_at DESC"
        ).fetchall()
        operations = {
            str(row[0]): str(row[1])
            for row in conn.execute("SELECT component,status FROM operational_status")
        }
        strategy_count = int(
            conn.execute("SELECT count(*) FROM strategy_versions").fetchone()[0]
        )
        factor_count = int(
            conn.execute("SELECT count(*) FROM factor_versions").fetchone()[0]
        )
        factor_evaluation_count = int(
            conn.execute(
                "SELECT count(*) FROM factor_evaluations WHERE status='COMPLETED'"
            ).fetchone()[0]
        )
        factor_evaluation_total = int(
            conn.execute("SELECT count(*) FROM factor_evaluations").fetchone()[0]
        )
        factor_definition_count = int(
            conn.execute("SELECT count(DISTINCT factor_id) FROM factor_versions").fetchone()[0]
        )
        factor_evaluation_statuses = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT status,count(*) FROM factor_evaluations GROUP BY status"
            ).fetchall()
        }
        factor_output_date = conn.execute(
            "SELECT max(json_extract(metrics_json,'$.evaluation_end')) "
            "FROM factor_evaluations WHERE status='COMPLETED'"
        ).fetchone()[0]
        research_output_date = conn.execute(
            "SELECT max(json_extract(j.value,'$.observed_end')) "
            "FROM research_runs r,json_each(r.window_json) j "
            "WHERE r.execution_scope='STRATEGY'"
        ).fetchone()[0]
        strategy_run_count = int(
            conn.execute(
                "SELECT count(*) FROM research_runs WHERE execution_scope='STRATEGY'"
            ).fetchone()[0]
        )
        independent_strategy_count = int(
            conn.execute(
                "SELECT count(*) FROM strategy_versions WHERE family_id <> 'CONTROL'"
            ).fetchone()[0]
        )
        non_control_stage10_pass = int(
            conn.execute(
                "SELECT count(*) FROM stage10_runs s JOIN research_runs r ON r.research_run_id=s.research_run_id "
                "WHERE r.execution_scope='STRATEGY' AND s.status='PASS'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    catalog_datasets, catalog_databases = _product_catalog_datasets(cfg)
    legacy_catalog = (
        {"datasets": catalog_datasets, "databases": catalog_databases}
        if catalog_datasets
        else _legacy_or({"datasets": [], "databases": []}, lambda: _legacy().catalog())
    )
    legacy_strategies = _legacy_or([], lambda: _legacy().strategies(500))
    legacy_factors = _legacy_or([], lambda: _legacy().factors(500))
    legacy_backtests = _legacy_or([], lambda: _legacy().backtests(limit=500))
    legacy_events_count = _legacy_or(0, lambda: int(_legacy().events(1).get("count", 0)))
    legacy_paper_accounts = _legacy_or([], lambda: _legacy().paper_accounts(500))
    meta = _meta()
    profile = _profile_response(cfg)
    latest_replay = _read_evidence(cfg, "REAL_OPERATION/historical_20d_replay.json")
    shadow = _read_evidence(cfg, "S10/shadow_status.json") or {}
    shadow_completed = int(shadow.get("recorded_sessions", 0))
    shadow_target = int(shadow.get("target_sessions", 20))
    coverage_summary = {
        dataset: {
            "rows": int(manifest.get("row_count", 0) or 0),
            "latest": str(manifest.get("end_date") or manifest.get("latest_date") or "—"),
            "status": str(manifest.get("quality_status") or "UNKNOWN"),
            "source": str(manifest.get("provenance") or "REAL"),
            "manifest_hash": manifest.get("manifest_hash"),
        }
        for dataset, manifest in publications
    }
    ai_config, ai_status = _ai_runtime()
    from src.quantcore.portfolio.paper_operations import (
        CURRENT_PAPER_PROFILE,
        current_paper_readiness,
        latest_supported_session,
    )

    all_v2_accounts = _v2_accounts(cfg)
    strategy_rows = _v2_strategy_rows(cfg)
    current_scope = _current_etf_scope(cfg)
    current_accounts = [
        item
        for item in all_v2_accounts
        if item.get("generation_id")
        and item.get("data_profile") == CURRENT_PAPER_PROFILE
        and item.get("status") in {"ACTIVE", "PAUSED"}
    ]
    paper_accounts = [
        item
        for item in current_accounts
        if item.get("status") == "ACTIVE"
    ]
    current_data_date_value = latest_supported_session(
        cfg, profile=CURRENT_PAPER_PROFILE
    )
    current_data_date = (
        current_data_date_value.isoformat() if current_data_date_value else None
    )
    execution_dates = [
        str(item["last_trade_date"])
        for item in paper_accounts
        if item.get("last_trade_date")
    ]
    paper_date = max(execution_dates) if execution_dates else None
    current_paper_plans: list[dict[str, Any]] = []
    for item in strategy_rows:
        if not item.get("paper_eligible"):
            continue
        readiness = current_paper_readiness(cfg, str(item["strategy_id"]))
        account = next(
            (
                candidate
                for candidate in all_v2_accounts
                if candidate.get("strategy_id") == item["strategy_id"]
                and candidate.get("data_profile") == CURRENT_PAPER_PROFILE
            ),
            None,
        )
        current_paper_plans.append(
            {
                "strategy_id": item["strategy_id"],
                "strategy_key": item.get("strategy_key"),
                "qualification": item.get("qualification"),
                "account_id": account.get("account_id") if account else None,
                "account_status": account.get("status") if account else None,
                "execution_allowed": bool(account and account.get("execution_allowed")),
                "readiness": readiness,
            }
        )
    current_paper_ready_to_start = bool(
        not current_accounts
        and current_scope.get("current_paper_lookback_ready")
        and current_paper_plans
        and all(plan["readiness"].get("ready_for_generation") for plan in current_paper_plans)
    )
    current_paper_pass = bool(paper_accounts) and all(
        item.get("continuity_status") == "PASS_TO_LATEST_SUPPORTED"
        for item in paper_accounts
    )
    current_paper_prepared = bool(paper_accounts) and all(
        item.get("continuity_status") == "READY_TO_RUN" and not item.get("last_trade_date")
        for item in paper_accounts
    )
    if current_paper_pass:
        current_paper_status = "PASS"
    elif current_paper_prepared:
        current_paper_status = "PREPARED_WAITING_FIRST_SESSION"
    elif current_paper_ready_to_start:
        current_paper_status = "READY_TO_CREATE"
    elif not current_scope.get("current_eod_ready"):
        current_paper_status = "WAITING_DATA"
    elif current_paper_plans:
        current_paper_status = "WAITING_DATA"
    else:
        current_paper_status = "NO_ELIGIBLE_STRATEGY"
    paper_usable = current_paper_pass or current_paper_prepared
    required_market = ("daily", "adj_factor", "stk_limit")
    market_dates = [
        str(coverage_summary[name]["latest"])
        for name in required_market
        if name in coverage_summary
        and str(coverage_summary[name]["latest"]).isdigit()
        and len(str(coverage_summary[name]["latest"])) == 8
    ]
    market_date = min(market_dates) if len(market_dates) == len(required_market) else None
    market_usable = market_date is not None
    research_date = coverage_summary.get("research_etf3_prices", {}).get("latest") or None
    factor_usable = factor_count > 0 and factor_evaluation_count > 0
    research_usable = strategy_run_count > 0 and non_control_stage10_pass > 0
    backtest_usable = strategy_run_count > 0 and non_control_stage10_pass > 0
    deepseek_usable = ai_status == "READY"
    stack_health = _stack_health(cfg)
    realtime_evidence = _read_evidence(cfg, "REAL_OPERATION/realtime_feed_check.json") or {}
    live_feed_status = str(
        realtime_evidence.get("status")
        or operations.get("intraday_live", "PENDING_VENDOR")
    )
    try:
        short_term_summary = ShortTermWorkbench().overview()
    except Exception as exc:
        short_term_summary = {"status": "BLOCKED", "reason": str(exc), "live_triggers_enabled": False}
    supported_daily_status = str(operations.get("supported_daily_loop", "PENDING"))
    daily_loop_usable = supported_daily_status == "PASS"
    # The product surface remains usable for local research and historical
    # backtests when optional AI, live feed, or current PAPER data is absent.
    # Those capabilities retain their own explicit gates below.
    product_usable = bool(
        (market_usable or current_scope.get("current_eod_ready"))
        and research_usable
        and backtest_usable
    )
    health_overall = str(stack_health.get("overall", "PENDING"))
    runtime_component_status = str(
        (stack_health.get("components") or {}).get("runtime", "PENDING")
    )
    runtime_card = "PASS" if runtime_component_status == "PASS" else runtime_component_status
    start_status = str(stack_health.get("one_click_start_stop", "PENDING"))
    vendor_status = "CONFIGURED" if RelayClient.configured() else "PENDING_VENDOR"
    paper_freshness = (
        "CURRENT"
        if current_paper_pass
        else "WAITING_FIRST_FORWARD_SESSION"
        if current_paper_prepared
        else "WAITING_CURRENT_DATA"
        if current_scope.get("current_eod_ready")
        else "WAITING_CURRENT_SCOPE"
    )
    module_status = {
        "PRODUCT": {
            "usable": product_usable,
            "status": "TRUE" if product_usable else "FALSE",
            "latest_date": market_date,
            "detail": "Local research/PAPER modules are independently gated; current Daily and realtime readiness are reported separately.",
        },
        "MARKET": {"usable": market_usable, "status": "PASS" if market_usable else "BLOCKED_DATA", "latest_date": market_date, "scope": "FULL_MARKET"},
        "ETF3_CURRENT": {
            "usable": bool(current_scope.get("current_eod_ready")),
            "status": "PASS" if current_scope.get("current_eod_ready") else "WAITING_DATA",
            "latest_date": current_scope.get("latest_complete_eod"),
            "business_date": current_scope.get("business_date"),
            "scope": current_scope.get("scope", "ETF3_CURRENT"),
            "codes": current_scope.get("codes", []),
            "missing_complete_sessions": current_scope.get("missing_complete_sessions", []),
        },
        "FACTOR_LAB": {"usable": factor_usable, "status": "PASS" if factor_usable else "BLOCKED_DATA", "latest_date": factor_output_date, "legal_input_date": research_date},
        "RESEARCH": {"usable": research_usable, "status": "PASS" if research_usable else "BLOCKED_DATA", "latest_date": research_output_date, "legal_input_date": research_date},
        "BACKTEST": {"usable": backtest_usable, "status": "PASS" if backtest_usable else "BLOCKED_DATA", "latest_date": research_output_date, "legal_input_date": research_date},
        "DEEPSEEK": {"usable": deepseek_usable, "status": "PASS" if deepseek_usable else ai_status, "latest_date": None, "provider": ai_config.provider},
        "PAPER": {
            "usable": paper_usable,
            "status": current_paper_status,
            "latest_date": paper_date,
            "latest_execution_date": paper_date,
            "freshness": paper_freshness,
            "data_status": "PASS" if current_paper_pass or current_paper_prepared or current_paper_ready_to_start else "WAITING_FORWARD_EOD",
            "detail": "Current PAPER is a forward-only ETF3 generation. Historical generations remain read-only and are not caught up.",
            "business_date": current_scope.get("business_date"),
            "data_date": current_data_date,
            "scope": CURRENT_PAPER_PROFILE,
        },
        "LIVE_WATCH_FEED": {"usable": live_feed_status == "PASS", "status": live_feed_status, "latest_date": None},
        "SHORT_TERM": {
            "usable": short_term_summary.get("status") in {"READY_DECISION_SUPPORT", "READY_NO_SCAN"},
            "status": short_term_summary.get("status", "BLOCKED"),
            "latest_date": (short_term_summary.get("latest_scan") or {}).get("data_date"),
            "target_session": (short_term_summary.get("latest_scan") or {}).get("target_session"),
            "live_triggers_enabled": False,
        },
        "DAILY_LOOP": {
            "usable": daily_loop_usable,
            "status": supported_daily_status,
            "latest_date": current_scope.get("latest_complete_eod"),
            "business_date": current_scope.get("business_date"),
            "scope": "ETF3_CURRENT_FORWARD",
            "snapshot": daily_truth["snapshot"],
            "run_id": daily_truth["run_id"],
            "completed_at": daily_truth["completed_at"],
            "detail": (
                "Current supported ETF3 Daily loop is independently gated; "
                f"full-market DAG remains {daily_truth['status']} and is visible in System."
            ),
            "full_market_status": daily_truth["status"],
        },
        "ONE_CLICK_START": {
            "usable": start_status == "PASS",
            "status": start_status,
            "latest_date": None,
            "detail": str(stack_health.get("detail", "one-click health evidence is not available")),
        },
        "FULL_STACK_HEALTH": {
            "usable": health_overall == "PASS",
            "status": health_overall,
            "latest_date": None,
            "detail": str(stack_health.get("detail", "stack health has not been verified by the launcher")),
        },
    }
    status_cards = {
        "Product Usable": "TRUE" if product_usable else "FALSE",
        "Canonical Data": "PASS" if market_usable else "BLOCKED_DATA",
        "Runtime": runtime_card,
        "Supported Universe": "PASS" if profile["validation"]["ready"] else "FAIL",
        "Full Scope": "PARTIAL",
        "Research": "PASS" if strategy_run_count else "PENDING",
        "Stage09": "PASS" if strategy_run_count else "PENDING",
        "Stage10": "PASS" if non_control_stage10_pass else "PENDING",
        "ETF3 Current": "PASS" if current_scope.get("current_eod_ready") else "WAITING_DATA",
        "PAPER": current_paper_status,
        "Watch": live_feed_status,
        "Vendor": vendor_status,
        "AI": ai_status,
    }
    return {
        **meta,
        "project": "Integrated V2",
        "version": "2",
        "timestamp": datetime.now(UTC).isoformat(),
        # ``_meta`` identifies the last published historical replay.  The
        # operational overview must expose today's resolved A-share business
        # date at top level; the replay remains explicitly separated below.
        "as_of": datetime.now(UTC).isoformat(),
        "business_date": daily_truth["business_date"],
        "state": "CURRENT_DAILY_OPERATION_WITH_HISTORICAL_REPLAY",
        "historical_replay": {
            "publication_id": meta.get("publication_id"),
            "business_date": meta.get("business_date"),
            "as_of": meta.get("as_of"),
            "state": meta.get("state"),
            "stale": True,
        },
        "evidence_refs": list(dict.fromkeys(
            list(meta.get("evidence_refs", []))
            + [f"run:{daily_truth['run_id']}", "evidence:FULL_ACTIVATION/stack_health.json"]
        )),
        "system_mode": "REAL HISTORICAL REPLAY",
        "full_scope_readiness": "PARTIAL",
        "supported_scope_operational_readiness": operations.get(
            "supported_daily_loop", "PENDING"
        ),
        "current_data_scope": current_scope,
        "current_paper": {
            "profile": CURRENT_PAPER_PROFILE,
            "status": current_paper_status,
            "usable": paper_usable,
            "data_date": current_data_date,
            "latest_execution_date": paper_date,
            "recommended_start_sessions": [
                plan["readiness"].get("requested_start_session")
                for plan in current_paper_plans
                if plan["readiness"].get("requested_start_session")
            ],
            "plans": current_paper_plans,
            "historical_continuity": "DEFERRED",
            "old_accounts_are_read_only": True,
            "reason": (
                next((str(plan["readiness"].get("reason")) for plan in current_paper_plans if plan["readiness"].get("reason")), "Current forward PAPER data is incomplete.")
                if current_paper_status == "WAITING_DATA"
                else "Current PAPER forward generation is operational."
            ),
        },
        "status_cards": status_cards,
        "product_usable": product_usable,
        "current_business_date": daily_truth["business_date"],
        "latest_complete_market_date": daily_truth["latest_complete_market_date"],
        "daily_operation": daily_truth,
        "full_stack_health": stack_health,
        "module_status": module_status,
        "supported_profile": {
            "profile_id": profile["profile_id"],
            "status": profile["validation"]["status"],
            "securities": profile["supported_securities"],
            "date_windows": profile["supported_date_windows"],
        },
        "boundaries": {
            "live_trading": False,
            "real_order_enabled": False,
            "local_only": True,
            "broker_enabled": False,
            "account_access": False,
            "broker_auth": False,
        },
        "data_source": meta["provenance"],
        "provenance_status": meta["quality_status"],
        "paper_runnable": paper_usable,
        "key_blockers": current_runtime_counts.get("WAITING_DATA", 0)
        + current_runtime_counts.get("DEGRADED", 0)
        + current_runtime_counts.get("FAILED", 0),
        "research_queue": queue,
        "coverage_summary": {
            **coverage_summary,
            "published_datasets": len(publications),
            "real_ready_manifests": int(curated_count or 0),
            "real_ready_rows": int(curated_rows or 0),
        },
        "data_domains": len(publications),
        "total_rows": int(curated_rows or 0),
        "factors": factor_count + len(legacy_factors),
        "strategies": strategy_count + len(legacy_strategies),
        "product_inventory": {
            "legacy_strategies": len(legacy_strategies),
            "legacy_factors": len(legacy_factors),
            "legacy_backtests": len(legacy_backtests),
            "legacy_events": legacy_events_count,
            "legacy_paper_accounts": len(legacy_paper_accounts),
            "v2_strategy_versions": strategy_count,
            "v2_research_runs": research_runs,
            "v2_factor_versions": factor_count,
            "v2_stage09_results": stage09_count,
            "v2_stage10_results": len(stage10_rows),
            "v2_strategy_runs": strategy_run_count,
            "v2_paper_accounts": len(all_v2_accounts),
            "v2_operational_paper_accounts": len(paper_accounts),
            "v2_audit_only_paper_accounts": sum(
                1
                for item in all_v2_accounts
                if not (
                    item.get("generation_id")
                    and item.get("data_profile") == CURRENT_PAPER_PROFILE
                    and item.get("status") in {"ACTIVE", "PAUSED"}
                )
            ),
            "v2_generation_count": sum(1 for item in all_v2_accounts if item.get("generation_id")),
        },
        "legacy_inventory": legacy_catalog,
        "latest_data_date": market_date or "",
        "runtime": {
            "persisted_jobs": sum(runtime_counts.values()),
            "status_counts": current_runtime_counts,
            "status_counts_all_history": runtime_counts,
            "scope": {
                "business_date": daily_truth["business_date"],
                "snapshot": daily_truth["snapshot"],
            },
        },
        "research": {
            "runs": research_runs,
            "stage09_results": stage09_count,
            "stage10_results": len(stage10_rows),
            "control_only": strategy_run_count == 0,
        },
        "stage10": {
            "latest_status": str(stage10_rows[0][2]) if stage10_rows else "PENDING",
            "pass_count": sum(1 for row in stage10_rows if row[2] == "PASS"),
            "exact_match": bool(stage10_rows and stage10_rows[0][4] == stage10_rows[0][5]),
        },
        "watch": {
            "engine_ready": True,
            "live_feed": live_feed_status,
            "feed_evidence": {
                "business_date": realtime_evidence.get("business_date"),
                "scope": realtime_evidence.get("scope"),
                "run_id": realtime_evidence.get("run_id"),
                "request_performed": realtime_evidence.get("request_performed", False),
            },
            "historical_replay": "DAILY_BAR_REPLAY_ONLY",
        },
        "short_term": short_term_summary,
        "ai": {
            "layer_ready": True,
            "provider": ai_config.provider,
            "model": ai_config.model,
            "deepseek": ai_status,
        },
        "shadow": {
            "completed": shadow_completed,
            "target": shadow_target,
            "display": f"{shadow_completed}/{shadow_target}",
        },
        "factor_summary": {
            "definition_count": factor_definition_count,
            "version_count": factor_count,
            "evaluation_count": factor_evaluation_total,
            "passed_count": factor_evaluation_count,
            "evaluation_statuses": factor_evaluation_statuses,
        },
        "strategy_summary": {
            "independent_strategy_count": independent_strategy_count,
            "strategy_version_count": strategy_count,
            "research_run_count": research_runs,
            "strategy_research_run_count": strategy_run_count,
            "duplicate_research_run_count": duplicate_research_run_count,
            "recovery_research_run_count": recovery_run_count,
        },
        "paper_summary": {
            "v2_total_accounts": len(all_v2_accounts),
            "v2_operational_accounts": len(current_accounts),
            "v2_current_active_accounts": len(paper_accounts),
            "v2_historical_accounts": sum(
                1 for item in all_v2_accounts if item not in current_accounts
            ),
            "legacy_audit_only_accounts": len(legacy_paper_accounts),
            "latest_execution_date": paper_date,
            "execution_data_status": "CURRENT" if current_scope.get("current_eod_ready") else "WAITING_DATA",
            "current_status": current_paper_status,
            "historical_continuity": "DEFERRED",
        },
        "latest_replay": latest_replay,
    }


@router.get("/research/factors")
def factors(limit: int = Query(200, le=500)):
    cfg = load_v2_config()
    actual_limit = limit if isinstance(limit, int) else 200
    v2_items = _v2_factor_items(cfg, actual_limit)
    legacy_items = _legacy_or([], lambda: _legacy().factors(actual_limit))
    combined = v2_items + legacy_items
    return _response(
        factors=combined,
        v2_factors=v2_items,
        accepted=[item["factor_id"] for item in v2_items if item.get("accepted")],
        total_registered=len(combined),
        legacy_count=len(legacy_items),
        v2_count=len(v2_items),
        state="V2_FACTOR_REGISTRY_AND_LEGACY_EVIDENCE",
        canonical_publication=bool(v2_items),
        message="V2 factors are evaluated from published canonical ETF3 prices; Legacy factors remain read-only evidence.",
    )


@router.get("/research/factors/{factor_id}")
def factor_detail(factor_id: str):
    cfg = load_v2_config()
    factor_items = _v2_factor_items(cfg, 500)
    v2_item = next(
        (item for item in factor_items if factor_id == item.get("factor_version_id")),
        None,
    )
    if v2_item is None:
        v2_item = next(
            (
                item
                for item in factor_items
                if factor_id == item.get("base_factor_id") and item.get("executable")
            ),
            None,
        )
    if v2_item is not None:
        conn = _connect(cfg.paths.state / "control.sqlite")
        try:
            values = [
                dict(row)
                for row in conn.execute(
                    "SELECT evaluation_id,on_date,asset_id,value,forward_return,available_at,input_hash "
                    "FROM factor_values WHERE factor_version_id=? ORDER BY on_date,asset_id LIMIT 500",
                    (v2_item["factor_version_id"],),
                )
            ]
            evaluations = [
                {
                    **dict(row),
                    "metrics": _parse_json(row["metrics_json"], {}),
                }
                for row in conn.execute(
                    "SELECT evaluation_id,snapshot_hash,oos_receipt_id,status,metrics_json,evidence_ref,created_at "
                    "FROM factor_evaluations WHERE factor_version_id=? ORDER BY created_at DESC LIMIT 20",
                    (v2_item["factor_version_id"],),
                )
            ]
        finally:
            conn.close()
        return _response(
            factor={**v2_item, "values": values, "evaluations": evaluations},
            factor_id=factor_id,
            state="V2_CANONICAL_FACTOR_DETAIL",
            paper_eligible=False,
            v2_revalidated=True,
        )
    item = _legacy_or(None, lambda: _legacy().factor_detail(factor_id))
    if item is None:
        raise HTTPException(404, detail={"state": "FACTOR_NOT_FOUND", "factor_id": factor_id})
    return _response(
        factor=item,
        factor_id=factor_id,
        state="LEGACY_EVIDENCE_DETAIL",
        paper_eligible=False,
        v2_revalidated=False,
    )


@router.get("/research/strategies")
def strategies():
    cfg = load_v2_config()
    items = _v2_strategy_rows(cfg)
    legacy_items = _legacy_or([], lambda: _legacy().strategies(500))
    combined = items + legacy_items
    return _response(
        strategies=combined,
        v2_strategies=items,
        legacy_strategies=legacy_items,
        paper_eligible=[item["strategy_id"] for item in items if item.get("paper_eligible")],
        legacy_count=len(legacy_items),
        v2_count=len(items),
        state="V2_REGISTRY_AND_LEGACY_EVIDENCE",
        message="Executable V2 strategies are bound to their own protocol, research run, Stage09/10 and qualification evidence; Legacy strategies remain read-only.",
    )


@router.get("/research/strategies/{strategy_id}")
def strategy_detail(strategy_id: str):
    cfg = load_v2_config()
    current = next(
        (item for item in _v2_strategy_rows(cfg) if item["strategy_id"] == strategy_id),
        None,
    )
    if current is not None:
        return _response(strategy=current, state="V2_CANONICAL_DETAIL")
    legacy_item = _legacy_or(None, lambda: _legacy().strategy_detail(strategy_id))
    if legacy_item is None:
        raise HTTPException(404, detail={"state": "STRATEGY_NOT_FOUND", "strategy_id": strategy_id})
    return _response(
        strategy=legacy_item,
        state="LEGACY_EVIDENCE_DETAIL",
        paper_eligible=False,
        v2_revalidated=False,
    )


@router.get("/research/overview")
def research_overview():
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        queue_rows = conn.execute(
            "SELECT q.queue_item_id,q.strategy_version_id,q.priority,q.status,q.attempt,q.updated_at,"
            "p.protocol_id,p.family_id,p.protocol_json "
            "FROM research_queue q JOIN research_protocols p ON p.protocol_hash=q.protocol_hash "
            "ORDER BY q.priority"
        ).fetchall()
        queue: list[dict[str, Any]] = []
        for row in queue_rows:
            assessment = conn.execute(
                "SELECT status,reasons_json,manifest_hashes_json,created_at "
                "FROM queue_readiness_assessments WHERE queue_item_id=? ORDER BY created_at DESC LIMIT 1",
                (row["queue_item_id"],),
            ).fetchone()
            protocol = json.loads(str(row["protocol_json"]))
            queue.append(
                {
                    "queue_item_id": row["queue_item_id"],
                    "protocol_id": row["protocol_id"],
                    "strategy_version_id": row["strategy_version_id"],
                    "family_id": row["family_id"],
                    "priority": row["priority"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "updated_at": row["updated_at"],
                    "dependency_set": dependency_set(protocol),
                    "reasons": json.loads(str(assessment["reasons_json"])) if assessment else [],
                    "manifest_hashes": json.loads(str(assessment["manifest_hashes_json"])) if assessment else {},
                    "assessed_at": assessment["created_at"] if assessment else None,
                }
            )
        run_rows = conn.execute(
            "SELECT research_run_id,queue_item_id,strategy_version_id,status,execution_scope,"
            "snapshot_hash,oos_receipt_id,dataset_manifest_json,window_json,cost_model,"
            "execution_rules,metrics_json,robustness_json,evidence_ref,created_at "
            "FROM research_runs ORDER BY created_at DESC"
        ).fetchall()
        runs = []
        for row in run_rows:
            runs.append(
                {
                    **dict(row),
                    "dataset_manifest": json.loads(str(row["dataset_manifest_json"])),
                    "windows": json.loads(str(row["window_json"])),
                    "metrics": json.loads(str(row["metrics_json"])),
                    "robustness": json.loads(str(row["robustness_json"])),
                }
            )
        stage09_rows = conn.execute(
            "SELECT r.result_id,r.queue_item_id,r.snapshot_hash,r.rating,r.reasons_json,"
            "r.primary_metric,r.effective_trades,r.validation_blocks,r.created_at,"
            "e.research_run_id,e.oos_receipt_id,e.metrics_json,e.robustness_json "
            "FROM stage09_results r LEFT JOIN stage09_evidence e ON e.result_id=r.result_id "
            "ORDER BY r.created_at DESC"
        ).fetchall()
        stage09 = []
        for row in stage09_rows:
            item = dict(row)
            item["reasons"] = json.loads(str(row["reasons_json"]))
            item["metrics"] = json.loads(str(row["metrics_json"])) if row["metrics_json"] else {}
            item["robustness"] = json.loads(str(row["robustness_json"])) if row["robustness_json"] else {}
            stage09.append(item)
        stage10_rows = conn.execute(
            "SELECT result_id,research_run_id,input_hash,formal_hash,reference_hash,status,"
            "evidence_valid,created_at FROM stage10_runs ORDER BY created_at DESC"
        ).fetchall()
        stage10 = [
            {
                **dict(row),
                "differences": int(
                    conn.execute(
                        "SELECT count(*) FROM stage10_differences WHERE result_id=?",
                        (row["result_id"],),
                    ).fetchone()[0]
                ),
                "exact_match": row["formal_hash"] == row["reference_hash"],
            }
            for row in stage10_rows
        ]
    finally:
        conn.close()
    legacy_queue = _legacy_or([], lambda: _legacy().research_queue(200))
    legacy_evaluations = _legacy_or([], lambda: _legacy().strategies(500))
    strategy_runs = [item for item in runs if item.get("execution_scope") == "STRATEGY"]
    control_runs = [item for item in runs if item.get("execution_scope") == "CONTROL"]
    paper_eligible = [
        item["strategy_id"]
        for item in _v2_strategy_rows(cfg)
        if item.get("paper_eligible")
    ]
    return _response(
        state="V2_AND_LEGACY_RESEARCH_EVIDENCE",
        queue=queue,
        legacy_queue=legacy_queue,
        runs=runs,
        stage09=stage09,
        stage10=stage10,
        legacy_evaluations=[
            item["evaluation"]
            for item in legacy_evaluations
            if item.get("evaluation") is not None
        ],
        paper_eligible=paper_eligible,
        control_only=not bool(strategy_runs),
        run_summary={
            "control_runs": len(control_runs),
            "strategy_runs": len(strategy_runs),
            "research_runs": len(runs),
            "executor_status": "STRATEGY_EXECUTOR_ACTIVE" if strategy_runs else "CONTROL_ONLY",
        },
        limitations=[
            "CONTROL runs are diagnostic only and cannot be promoted; STRATEGY runs use the registered ETF3 executor.",
            "Only the bounded ETF3 price/limit/master scope is executable; unsupported stock families remain dependency-gated.",
            "Legacy queue and evaluation records are historical evidence only and cannot be executed directly.",
        ],
    )


@router.get("/research/backtests")
def backtests():
    cfg = load_v2_config()
    legacy_items = _legacy_or([], lambda: _legacy().backtests(limit=200))
    v2_items: list[dict[str, Any]] = []
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        run_ids = [
            str(row["research_run_id"])
            for row in conn.execute(
                "SELECT research_run_id FROM research_runs WHERE execution_scope='STRATEGY' "
                "ORDER BY created_at DESC LIMIT 200"
            )
        ]
    finally:
        conn.close()
    for run_id in run_ids:
        item = _v2_run_detail(cfg, f"v2-run-{run_id}")
        if item is not None:
            v2_items.append(item)
    replay = _read_evidence(load_v2_config(), "REAL_OPERATION/historical_20d_replay.json")
    if replay:
        v2_items.append(
            {
                "bt_id": f"v2-{replay.get('scope', 'ETF3_KNOWN')}-{replay.get('sessions', [''])[0]}-{replay.get('sessions', [''])[-1]}",
                "backtest_id": f"v2-{replay.get('scope', 'ETF3_KNOWN')}-{replay.get('sessions', [''])[0]}-{replay.get('sessions', [''])[-1]}",
                "strategy_id": replay.get("strategy", "etf_rotation.v1"),
                "strategy_name": replay.get("strategy", "etf_rotation.v1"),
                "start": (replay.get("sessions") or [None])[0],
                "end": (replay.get("sessions") or [None])[-1],
                "created_at": replay.get("generated_at"),
                "engine": "quantcore.formal",
                "engine_version": "V2",
                "uses_oos": True,
                "n_days": replay.get("session_count", 0),
                "total_return": None,
                "bench_return": None,
                "legacy_status": "V2_REVALIDATED_REPLAY",
                "provenance": "REAL",
                "v2_revalidated": True,
                "stage10_status": replay.get("stage10_status"),
                "stage10_differences": replay.get("stage10_differences", 0),
                "evidence_ref": "evidence:REAL_OPERATION/historical_20d_replay.json",
            }
        )
    return _response(
        backtests=v2_items + legacy_items,
        v2_backtests=v2_items,
        legacy_backtests=legacy_items,
        v2_count=len(v2_items),
        legacy_count=len(legacy_items),
        state="V2_AND_LEGACY_RESULT_EVIDENCE",
        message="Legacy backtests are LEGACY_RESULT and are never silently converted into V2 Stage09 evidence.",
    )


@router.get("/research/backtests/{backtest_id}/nav")
def backtest_nav(backtest_id: str):
    v2_item = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_item is not None:
        return _response(
            backtest=v2_item,
            series=v2_item.get("nav_series", []),
            rows=v2_item.get("nav_series", []),
            state="V2_STRATEGY_RUN_NAV",
        )
    item = _legacy_or(None, lambda: _legacy().backtest_nav(backtest_id))
    if item is not None:
        return _response(**item)
    raise HTTPException(
        404,
        detail={"state": "BACKTEST_NOT_FOUND", "backtest_id": backtest_id},
    )


def _v2_replay_id(replay: dict[str, Any]) -> str:
    sessions = replay.get("sessions") or ["", ""]
    return f"v2-{replay.get('scope', 'ETF3_KNOWN')}-{sessions[0]}-{sessions[-1]}"


def _v2_replay_detail() -> dict[str, Any] | None:
    cfg = load_v2_config()
    replay = _read_evidence(cfg, "REAL_OPERATION/historical_20d_replay.json")
    if not replay:
        return None
    return {
        "backtest_id": _v2_replay_id(replay),
        "bt_id": _v2_replay_id(replay),
        "strategy_id": replay.get("strategy", "etf_rotation.v1"),
        "strategy_name": replay.get("strategy", "etf_rotation.v1"),
        "start": (replay.get("sessions") or [None])[0],
        "end": (replay.get("sessions") or [None])[-1],
        "engine": "quantcore.formal",
        "engine_version": "V2",
        "provenance": "REAL",
        "legacy_status": "V2_REVALIDATED_REPLAY",
        "v2_revalidated": True,
        "stage10_status": replay.get("stage10_status"),
        "stage10_result_id": replay.get("stage10_result_id"),
        "stage10_differences": replay.get("stage10_differences", 0),
        "formal_hash": replay.get("formal_hash"),
        "reference_hash": replay.get("reference_hash"),
        "fills": replay.get("fills", 0),
        "final_nav": replay.get("final_nav"),
        "total_return": replay.get("total_return"),
        "nav_series": replay.get("nav_series", []),
        "daily_snapshots": replay.get("daily_snapshots", []),
        "orders_detail": replay.get("orders_detail", []),
        "fills_detail": replay.get("fills_detail", []),
        "evidence_ref": "evidence:REAL_OPERATION/historical_20d_replay.json",
        "metrics": {
            "session_count": replay.get("session_count", 0),
            "final_nav": replay.get("final_nav"),
            "total_return": replay.get("total_return"),
        },
    }


@router.get("/backtests")
def product_backtests(strategy_id: str | None = None, limit: int = Query(100, le=500)):
    response = backtests()
    items = response.get("backtests", [])
    if strategy_id:
        items = [item for item in items if item.get("strategy_id") == strategy_id]
    actual_limit = limit if isinstance(limit, int) else 100
    heavy_fields = {
        "daily_snapshots",
        "nav_series",
        "orders_detail",
        "fills_detail",
        "signals",
        "windows",
        "rebalance_days",
    }
    summaries = [
        {key: value for key, value in item.items() if key not in heavy_fields}
        for item in items[:actual_limit]
    ]
    return _response(
        backtests=summaries,
        v2_count=response.get("v2_count", 0),
        legacy_count=response.get("legacy_count", 0),
        state=response.get("state"),
        message=response.get("message"),
    )


@router.get("/backtests/{backtest_id}")
def product_backtest_detail(backtest_id: str):
    v2_run = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_run is not None:
        return _response(backtest=v2_run, state="V2_STRATEGY_RUN_DETAIL")
    v2_item = _v2_replay_detail()
    if v2_item and v2_item["backtest_id"] == backtest_id:
        return _response(backtest=v2_item, state="V2_REVALIDATED_REPLAY_DETAIL")
    item = _legacy_or(None, lambda: _legacy().backtest_detail(backtest_id))
    if item is None:
        raise HTTPException(404, detail={"state": "BACKTEST_NOT_FOUND", "backtest_id": backtest_id})
    return _response(backtest=item, state="LEGACY_RESULT_DETAIL")


@router.get("/backtests/{backtest_id}/nav")
def product_backtest_nav(backtest_id: str):
    v2_run = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_run is not None:
        return _response(
            backtest=v2_run,
            series=v2_run.get("nav_series", []),
            rows=v2_run.get("nav_series", []),
            state="V2_STRATEGY_RUN_NAV",
            message="NAV curve is serialized from the persisted formal strategy run.",
        )
    v2_item = _v2_replay_detail()
    if v2_item and v2_item["backtest_id"] == backtest_id:
        return _response(
            backtest=v2_item,
            series=v2_item.get("nav_series", []),
            rows=v2_item.get("nav_series", []),
            state="V2_REPLAY_NAV",
            message="NAV curve is serialized from the exact-match formal replay snapshots.",
        )
    item = _legacy_or(None, lambda: _legacy().backtest_nav(backtest_id))
    if item is None:
        raise HTTPException(404, detail={"state": "BACKTEST_NOT_FOUND", "backtest_id": backtest_id})
    return _response(**item, state="LEGACY_RESULT_NAV")


@router.get("/backtests/{backtest_id}/fills")
def product_backtest_fills(backtest_id: str, limit: int = Query(5000, le=10000)):
    actual_limit = limit if isinstance(limit, int) else 5000
    v2_run = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_run is not None:
        rows = v2_run.get("fills_detail", [])
        return _response(fills=rows[:actual_limit], count=len(rows), state="V2_STRATEGY_RUN_FILLS")
    v2_item = _v2_replay_detail()
    if v2_item and v2_item["backtest_id"] == backtest_id:
        rows = v2_item.get("fills_detail", [])
        return _response(fills=rows[:actual_limit], count=len(rows), state="V2_REPLAY_FILLS")
    rows = _legacy_or([], lambda: _legacy().backtest_fills(backtest_id, actual_limit))
    return _response(fills=rows, count=len(rows), state="LEGACY_RESULT_FILLS")


@router.get("/backtests/{backtest_id}/trades")
def product_backtest_trades(backtest_id: str, limit: int = Query(5000, le=10000)):
    actual_limit = limit if isinstance(limit, int) else 5000
    v2_run = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_run is not None:
        rows = v2_run.get("fills_detail", [])
        return _response(trades=rows[:actual_limit], count=len(rows), state="V2_STRATEGY_RUN_TRADES")
    v2_item = _v2_replay_detail()
    if v2_item and v2_item["backtest_id"] == backtest_id:
        rows = v2_item.get("fills_detail", [])
        return _response(trades=rows[:actual_limit], count=len(rows), state="V2_REPLAY_TRADES")
    rows = _legacy_or([], lambda: _legacy().backtest_trades(backtest_id, actual_limit))
    return _response(trades=rows, count=len(rows), state="LEGACY_RESULT_TRADES")


@router.get("/backtests/{backtest_id}/orders")
def product_backtest_orders(backtest_id: str, limit: int = Query(5000, le=10000)):
    actual_limit = limit if isinstance(limit, int) else 5000
    v2_run = _v2_run_detail(load_v2_config(), backtest_id)
    if v2_run is not None:
        rows = v2_run.get("orders_detail", [])
        return _response(orders=rows[:actual_limit], count=len(rows), state="V2_STRATEGY_RUN_ORDERS")
    v2_item = _v2_replay_detail()
    if v2_item and v2_item["backtest_id"] == backtest_id:
        rows = v2_item.get("orders_detail", [])
        return _response(orders=rows[:actual_limit], count=len(rows), state="V2_REPLAY_ORDERS")
    rows = _legacy_or([], lambda: _legacy().backtest_orders(backtest_id, actual_limit))
    return _response(orders=rows, count=len(rows), state="LEGACY_RESULT_ORDERS")


@router.get("/research-queue")
def research_queue():
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = [
            dict(x)
            for x in conn.execute(
                "SELECT q.queue_item_id,q.strategy_version_id,q.priority,q.status,q.attempt,q.updated_at,"
                "p.protocol_id,p.family_id,p.protocol_json FROM research_queue q "
                "JOIN research_protocols p ON p.protocol_hash=q.protocol_hash ORDER BY q.priority LIMIT 100"
            )
        ]
    finally:
        conn.close()
    for row in rows:
        protocol = json.loads(str(row.pop("protocol_json")))
        row["dependency_set"] = dependency_set(protocol)
    legacy_rows = _legacy_or([], lambda: _legacy().research_queue(200))
    return _response(
        queue=rows + legacy_rows,
        v2_queue=rows,
        legacy_queue=legacy_rows,
        state="V2_AND_LEGACY_QUEUE",
        message="Legacy queue items are visible for audit and cannot be executed directly.",
    )


@router.get("/research/queue")
def research_queue_page():
    return research_queue()


@router.get("/research/evaluations")
def evaluations(strategy_id: str | None = None, limit: int = Query(100, le=500)):
    actual_limit = limit if isinstance(limit, int) else 100
    cfg = load_v2_config()
    v2_values: list[dict[str, Any]] = []
    for item in _v2_strategy_rows(cfg):
        if strategy_id and strategy_id not in {item.get("strategy_id"), item.get("strategy_key")}:
            continue
        if item.get("research_run"):
            v2_values.append(
                {
                    "strategy_id": item["strategy_id"],
                    "strategy_key": item.get("strategy_key"),
                    "strategy_version_id": item["strategy_version_id"],
                    "family_id": item["family_id"] if "family_id" in item else item.get("category"),
                    "research_run_id": item["research_run"].get("research_run_id"),
                    "status": item["research_run"].get("status"),
                    "rating": item.get("rating"),
                    "stage09": item.get("stage09"),
                    "stage10": item.get("stage10"),
                    "metrics": item.get("stage09_metrics") or _parse_json(item["research_run"].get("metrics_json"), {}),
                    "evidence_ref": item["research_run"].get("evidence_ref"),
                    "provenance": "V2_CANONICAL",
                    "paper_eligible": item.get("paper_eligible", False),
                }
            )
    legacy_items = _legacy_or([], lambda: _legacy().strategies(500))
    legacy_values = [
        item["evaluation"]
        for item in legacy_items
        if item.get("evaluation") is not None
        and (not strategy_id or item.get("strategy_id") == strategy_id)
    ]
    values = v2_values + legacy_values
    return _response(
        evaluations=values[:actual_limit],
        count=len(values),
        state="V2_AND_LEGACY_EVALUATIONS",
        v2_evaluations=v2_values[:actual_limit],
        legacy_evaluations=legacy_values[:actual_limit],
    )


@router.get("/research/studies")
def studies(strategy_id: str | None = None, limit: int = Query(100, le=500)):
    actual_limit = limit if isinstance(limit, int) else 100
    values = _legacy_or([], lambda: _legacy().strategies(500))
    studies_out: list[dict[str, Any]] = []
    adapter = _legacy()
    try:
        with adapter._db("lab.db") as conn:
            for item in values:
                if strategy_id and item.get("strategy_id") != strategy_id:
                    continue
                rows = adapter._rows(
                    conn,
                    "SELECT * FROM study WHERE strategy_id=? ORDER BY created_at DESC LIMIT ?",
                    (item.get("strategy_id"), actual_limit),
                )
                studies_out.extend(rows)
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.DatabaseError):
        studies_out = []
    return _response(
        studies=studies_out[:actual_limit],
        count=len(studies_out),
        state="LEGACY_EVIDENCE_ONLY",
        v2_studies=[],
    )


@router.get("/research/oos")
def oos_registry(strategy_id: str | None = None, limit: int = Query(100, le=500)):
    actual_limit = limit if isinstance(limit, int) else 100
    adapter = _legacy()
    values: list[dict[str, Any]] = []
    try:
        with adapter._db("lab.db") as conn:
            where = ""
            params: list[object] = []
            if strategy_id:
                where = "WHERE strategy_id=?"
                params.append(strategy_id)
            values = adapter._rows(
                conn,
                "SELECT * FROM oos_registry "
                + where
                + " ORDER BY created_at DESC LIMIT ?",
                (*params, actual_limit),
            )
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.DatabaseError):
        values = []
    for row in values:
        row.update(
            {
                "provenance": LEGACY_PROVENANCE,
                "state": "LEGACY_EVIDENCE_ONLY",
                "promotion_allowed": False,
            }
        )
    return _response(oos_registry=values, count=len(values), v2_oos_registry=[])


@router.get("/market/daily")
def market_daily(
    ts_code: str = Query(...), start_date: str = Query(""), end_date: str = Query("")
):
    # Merge canonical sources first.  ``daily_price_only`` is a public
    # Sina/Tencent display supplement: it has real unadjusted OHLC but cannot
    # provide adjustment, PIT, limit or suspension truth.  The local Legacy
    # archive may extend history, but it never overrides a canonical key.
    sources = (
        ("etf3_current_prices", "REAL_READY_CURRENT_ETF_SCOPE"),
        ("known_etf3_prices", "REAL_READY_KNOWN_HISTORICAL"),
        ("etf3_continuous_prices", "REAL_READY_CONTINUOUS_ETF"),
        ("research_etf3_prices", "REAL_READY_RESEARCH_CANONICAL"),
        ("fund_daily", "REAL_READY_CANONICAL_FUND_DAILY"),
        # Prefer the complete canonical daily snapshot on overlapping dates;
        # the public PRICE_ONLY supplement may only extend newer sessions.
        ("daily", "REAL_READY_CANONICAL_DAILY_SNAPSHOT"),
        ("daily_price_only", "REAL_READY_PRICE_ONLY_SUPPLEMENT"),
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    manifest_hashes: dict[str, str] = {}
    states: list[str] = []
    datasets_used: list[str] = []
    for dataset, state in sources:
        manifest, rows = _publication_rows(dataset)
        selected = [
            row
            for row in rows
            if str(row.get("ts_code")) == ts_code
            and (not start_date or str(row.get("trade_date")) >= start_date)
            and (not end_date or str(row.get("trade_date")) <= end_date)
        ]
        if not selected:
            continue
        datasets_used.append(dataset)
        states.append(state)
        if manifest.get("manifest_hash"):
            manifest_hashes[dataset] = str(manifest["manifest_hash"])
        for row in selected:
            key = (str(row.get("ts_code")), str(row.get("trade_date")))
            merged.setdefault(
                key,
                {
                    **row,
                    "provenance": "REAL",
                    "publication_state": state,
                    "source_dataset": dataset,
                },
            )
    legacy = _legacy_or(
        None, lambda: _legacy().daily(ts_code, start_date, end_date, limit=5000)
    )
    legacy_rows = list((legacy or {}).get("rows") or [])
    legacy_used = False
    for row in legacy_rows:
        key = (str(row.get("ts_code")), str(row.get("trade_date")))
        if key not in merged:
            merged[key] = row
            legacy_used = True
    if merged:
        ordered = sorted(merged.values(), key=lambda item: str(item.get("trade_date")))
        available_dates = sorted({str(item.get("trade_date")) for item in ordered})
        current_included = "etf3_current_prices" in datasets_used
        price_only_included = "daily_price_only" in datasets_used
        canonical_included = bool(datasets_used)
        if current_included:
            state = "REAL_READY_CURRENT_ETF_SCOPE"
            price_basis = "UNADJUSTED_EXECUTION_PRICE"
        elif price_only_included and legacy_used:
            state = "REAL_PRICE_ONLY_WITH_LEGACY_HISTORY_FALLBACK"
            price_basis = "UNADJUSTED_PRICE_ONLY_WITH_LEGACY_HISTORY_UNVERIFIED"
        elif price_only_included:
            state = "REAL_READY_PRICE_ONLY"
            price_basis = "UNADJUSTED_PRICE_ONLY"
        elif canonical_included:
            state = "REAL_READY_MERGED_CANONICAL"
            price_basis = "UNADJUSTED_REPORTED_PRICE"
        else:
            state = str((legacy or {}).get("state") or "LEGACY_READONLY_HISTORICAL")
            price_basis = str(
                (legacy or {}).get("price_basis")
                or "LEGACY_REPORTED_PRICE_UNVERIFIED_FOR_V2_EXECUTION"
            )
        return _response(
            ts_code=ts_code,
            dataset=(
                datasets_used[0]
                if len(datasets_used) == 1 and not legacy_used
                else "MERGED_CANONICAL_AND_HISTORY"
            ),
            datasets_used=datasets_used,
            rows=ordered,
            data=ordered,
            count=len(ordered),
            available_dates=available_dates,
            date_range={"start": available_dates[0], "end": available_dates[-1]},
            state=state,
            source_states=states,
            price_basis=price_basis,
            manifest_hash=(
                manifest_hashes.get(datasets_used[0]) if datasets_used else (legacy or {}).get("manifest_hash")
            ),
            manifest_hashes=manifest_hashes,
            provenance=("MIXED" if canonical_included and legacy_used else "REAL" if canonical_included else LEGACY_PROVENANCE),
            legacy_history_fallback=legacy_used,
            execution_eligible=current_included,
            research_authorized=current_included,
            message=(
                "Public Sina/Tencent rows are unadjusted PRICE_ONLY display data; unknown execution/PIT fields remain null and do not authorize research or PAPER."
                if price_only_included
                else (legacy or {}).get("message")
            ),
            realtime_semantics=False,
        )
    return _response(
        ts_code=ts_code,
        rows=[],
        data=[],
        count=0,
        available_dates=[],
        date_range=None,
        state="NO_CANONICAL_MARKET_PUBLICATION",
        price_basis="UNADJUSTED_EXECUTION_PRICE_UNAVAILABLE",
        provenance="UNKNOWN",
        realtime_semantics=False,
    )


@router.get("/market/securities")
def securities(q: str = Query(""), limit: int = Query(200, le=1000)):
    manifest, rows = _publication_rows("historical_etf_master")
    needle = (q if isinstance(q, str) else "").casefold().strip()
    actual_limit = limit if isinstance(limit, int) else 200
    selected = [
        row
        for row in rows
        if not needle
        or needle in str(row.get("ts_code") or "").casefold()
        or needle in str(row.get("name") or "").casefold()
    ][:actual_limit]
    legacy = _legacy_or([], lambda: _legacy().security_search(q, limit))
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected + legacy:
        code = str(item.get("ts_code") or "")
        if code and code not in seen:
            seen.add(code)
            combined.append(item)
    return _response(
        securities=combined[:limit],
        data=combined[:limit],
        count=len(combined[:limit]),
        canonical_count=len(selected),
        legacy_count=len([x for x in combined if x.get("provenance") == LEGACY_PROVENANCE]),
        state=(
            "CANONICAL_AND_LEGACY_EVIDENCE"
            if combined
            else "NO_MATCHING_SECURITY"
        ),
        manifest_hash=manifest.get("manifest_hash"),
        legacy_readonly=True,
        message="Legacy securities are lazy read-only evidence; they are not copied into V2 SQLite.",
    )


@router.get("/market/security")
def market_security(ts_code: str):
    detail = _legacy_or(None, lambda: _legacy().security_detail(ts_code))
    if detail is None:
        manifest, rows = _publication_rows("historical_etf_master")
        item = next((x for x in rows if x.get("ts_code") == ts_code), None)
        if item is None:
            raise HTTPException(404, detail={"state": "SECURITY_NOT_FOUND", "ts_code": ts_code})
        detail = {
            **item,
            "provenance": "REAL",
            "publication": manifest,
            "coverage": {"rows": 0, "start": None, "end": None},
        }
    return _response(security=detail, ts_code=ts_code, state="SECURITY_DETAIL")


@router.get("/market/benchmarks")
def market_benchmarks(limit: int = Query(100, le=500)):
    values = _legacy_or([], lambda: _legacy().benchmarks(limit))
    return _response(
        benchmarks=values,
        count=len(values),
        state="LEGACY_EVIDENCE_AVAILABLE",
        v2_benchmarks=[],
    )


@router.get("/market/predict")
def market_predict(ts_code: str = Query(...)):
    value = _legacy_or(
        {
            "ts_code": ts_code,
            "state": "BLOCKED_DATA",
            "research_only": True,
            "experimental": True,
        },
        lambda: _legacy().predict(ts_code),
    )
    return _response(
        prediction=value,
        state="EXPERIMENTAL_RESEARCH_ONLY",
        trading_signal=False,
        paper_eligible=False,
        message="Historical diagnostic only; it cannot create a strategy, PAPER qualification, or order.",
    )


@router.get("/paper/portfolio")
def paper():
    cfg = load_v2_config()
    from src.quantcore.portfolio.paper_operations import (
        CURRENT_PAPER_PROFILE,
        current_paper_readiness,
        latest_supported_session,
        v2_accounts,
    )

    accounts = v2_accounts(cfg)
    current_scope = _current_etf_scope(cfg)
    latest_supported = latest_supported_session(cfg, profile=CURRENT_PAPER_PROFILE)
    strategy_rows = _v2_strategy_rows(cfg)
    current_accounts = [
        item
        for item in accounts
        if item.get("generation_id")
        and item.get("data_profile") == CURRENT_PAPER_PROFILE
        and item.get("status") in {"ACTIVE", "PAUSED"}
    ]
    operational_accounts = [
        item for item in current_accounts if item.get("status") == "ACTIVE"
    ]
    current_paper_plans: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for item in strategy_rows:
        if not item.get("paper_eligible"):
            continue
        readiness = current_paper_readiness(cfg, str(item["strategy_id"]))
        account = next(
            (
                candidate
                for candidate in accounts
                if candidate.get("strategy_id") == item["strategy_id"]
                and candidate.get("data_profile") == CURRENT_PAPER_PROFILE
            ),
            None,
        )
        plan = {
            "strategy_id": item["strategy_id"],
            "strategy_key": item.get("strategy_key"),
            "qualification": item.get("qualification"),
            "account_id": account.get("account_id") if account else None,
            "account_status": account.get("status") if account else None,
            "execution_allowed": bool(account and account.get("execution_allowed")),
            "readiness": readiness,
        }
        current_paper_plans.append(plan)
        eligible.append(
            {
                "strategy_id": item["strategy_id"],
                "strategy_key": item.get("strategy_key"),
                "qualification": item.get("qualification"),
                "account_id": plan["account_id"],
                "paper_eligible": True,
                "executable": item.get("executable", False),
                "current_paper": readiness,
            }
        )
    legacy_accounts = _legacy_or([], lambda: _legacy().paper_accounts(100))
    legacy_normalized: list[dict[str, Any]] = []
    for legacy_account in legacy_accounts:
        initial_cash = float(legacy_account.get("initial_cash") or 0)
        nav_value = float(legacy_account.get("nav") or 0)
        legacy_normalized.append(
            {
                **legacy_account,
                "strategy_id": legacy_account.get("strategy_id"),
                "total_return": nav_value / initial_cash - 1
                if initial_cash
                else None,
                "as_of": legacy_account.get("updated_at"),
                "reconciliation_status": legacy_account.get(
                    "reconciliation_status", "LEGACY_READONLY_NOT_RECONCILED_IN_V2"
                ),
            }
        )
    all_accounts = accounts + legacy_normalized
    paper_pass = bool(operational_accounts) and all(
        item.get("continuity_status") == "PASS_TO_LATEST_SUPPORTED"
        for item in operational_accounts
    )
    paper_prepared = bool(operational_accounts) and all(
        item.get("continuity_status") == "READY_TO_RUN" and not item.get("last_trade_date")
        for item in operational_accounts
    )
    paper_usable = paper_pass or paper_prepared
    execution_dates = [
        str(item["last_trade_date"])
        for item in operational_accounts
        if item.get("last_trade_date")
    ]
    latest_execution_date = max(execution_dates) if execution_dates else None
    current_paper_status = (
        "PASS"
        if paper_pass
        else "PREPARED_WAITING_FIRST_SESSION"
        if paper_prepared
        else "READY_TO_CREATE"
        if not current_accounts
        and current_scope.get("current_paper_lookback_ready")
        and any(plan["readiness"].get("ready_for_generation") for plan in current_paper_plans)
        else "WAITING_DATA"
        if current_paper_plans
        else "NO_ELIGIBLE_STRATEGY"
    )
    return _response(
        message="Current forward PAPER portfolio; historical accounts are read-only evidence",
        paper_eligible_only=False,
        live_trading=False,
        state=(
            "PAPER_ACTIVE_CURRENT"
            if operational_accounts
            else "PAPER_WAITING_CURRENT_DATA"
            if current_paper_plans
            else "PAPER_V2_AUDIT_ONLY"
            if accounts
            else "PAPER_LEGACY_READONLY_VISIBLE"
            if legacy_normalized
            else "PAPER_READY_NO_ELIGIBLE_STRATEGY"
        ),
        eligibility_message=(
            "Eligible strategies are waiting for the complete causal ETF3 lookback; no account is created from stale history."
            if current_paper_status == "WAITING_DATA"
            else "Only ACTIVE ETF3_FORWARD_CURRENT generations are executable; historical V2 and Legacy accounts are read-only."
        ),
        paper_engine_ready=True,
        paper_usable=paper_usable,
        current_paper_status=current_paper_status,
        execution_data_freshness=(
            "CURRENT_DATA_READY" if current_scope.get("current_eod_ready") else "WAITING_CURRENT_DATA"
        ),
        execution_data_status=(
            "PASS" if current_scope.get("current_eod_ready") else "WAITING_DATA"
        ),
        latest_execution_date=latest_execution_date,
        accounts=all_accounts,
        v2_accounts=accounts,
        legacy_accounts=legacy_normalized,
        operational_v2_accounts=operational_accounts,
        audit_only_v2_accounts=[
            item for item in accounts if item not in operational_accounts
        ],
        account_summary={
            "v2_total": len(accounts),
            "v2_operational": len(operational_accounts),
            "v2_audit_only": len(accounts) - len(operational_accounts),
            "legacy_audit_only": len(legacy_normalized),
        },
        latest_supported_date=latest_supported.isoformat() if latest_supported else None,
        current_data_scope=current_scope,
        current_paper_plans=current_paper_plans,
        historical_continuity="DEFERRED",
        eligible_strategies=eligible,
        operation_policy={
            "legacy_accounts": "READ_ONLY",
            "historical_v2_generations": "READ_ONLY_FROZEN",
            "current_profile": CURRENT_PAPER_PROFILE,
            "create_generation_requires_complete_causal_lookback": True,
            "manual_force": False,
            "real_orders": False,
        },
    )


@router.get("/paper/accounts")
def paper_accounts():
    return paper()


@router.get("/paper/{strategy_id}")
def paper_detail(strategy_id: str):
    current = paper()
    item = next(
        (x for x in current.get("accounts", []) if x.get("strategy_id") == strategy_id),
        None,
    )
    if item is None:
        eligible = next(
            (x for x in current.get("eligible_strategies", []) if x.get("strategy_id") == strategy_id),
            None,
        )
        if eligible is not None:
            return _response(
                strategy=eligible,
                state=(
                    "V2_PAPER_ELIGIBLE_WAITING_CURRENT_DATA"
                    if (eligible.get("current_paper") or {}).get("status") != "PASS"
                    else "V2_PAPER_ELIGIBLE_NOT_ADMITTED"
                ),
                execution_allowed=False,
                real_orders=False,
            )
        raise HTTPException(404, detail={"state": "PAPER_STRATEGY_NOT_FOUND", "strategy_id": strategy_id})
    return _response(
        strategy=item,
        state="LEGACY_READONLY_PAPER_DETAIL"
        if item.get("legacy_status") == LEGACY_PAPER
        else "V2_PAPER_DETAIL",
        execution_allowed=item.get("legacy_status") != LEGACY_PAPER,
    )


@router.get("/paper/{strategy_id}/{detail_type}")
def paper_detail_collection(
    strategy_id: str, detail_type: str, limit: int = Query(200, le=1000)
):
    cfg = load_v2_config()
    account = next(
        (x for x in _v2_accounts(cfg) if x.get("strategy_id") == strategy_id),
        None,
    )
    if account is not None:
        account_id = str(account["account_id"])
        current_generation = bool(
            account.get("generation_id")
            and account.get("data_profile") == "ETF3_FORWARD_CURRENT"
            and account.get("status") in {"ACTIVE", "PAUSED"}
        )
        field = {
            "nav": "nav_series",
            "positions": "positions",
        }.get(detail_type, detail_type)
        values: list[dict[str, Any]] = []
        if field in {"nav_series", "positions"}:
            raw = account.get(field, [])
            values = [dict(x) for x in raw if isinstance(x, dict)]
        else:
            conn = _connect(cfg.paths.state / "accounting.sqlite")
            try:
                if field == "orders":
                    values = [dict(x) for x in conn.execute("SELECT * FROM orders WHERE account_id=? ORDER BY created_on,order_id LIMIT ?", (account_id, limit))]
                elif field in {"fills", "trades"}:
                    values = [dict(x) for x in conn.execute("SELECT * FROM fills WHERE account_id=? ORDER BY trade_on,fill_id LIMIT ?", (account_id, limit))]
                elif field == "ledger":
                    values = []
                    for entry in conn.execute("SELECT entry_id,event_id,event_on FROM journal_entries WHERE account_id=? ORDER BY event_on,entry_id LIMIT ?", (account_id, limit)):
                        values.append({
                            **dict(entry),
                            "lines": [dict(line) for line in conn.execute("SELECT line_no,ledger_account,debit,credit FROM journal_lines WHERE account_id=? AND entry_id=? ORDER BY line_no", (account_id, entry["entry_id"]))],
                        })
            finally:
                conn.close()
        if field == "signals":
            conn = _connect(cfg.paths.state / "control.sqlite")
            try:
                receipt_table = (
                    "paper_generation_operation_receipts"
                    if account.get("generation_id")
                    else "paper_operation_receipts"
                )
                receipts = conn.execute(
                    f"SELECT result_json FROM {receipt_table} WHERE account_id=? AND action IN ('STEP','STEP_SPAN') ORDER BY created_at LIMIT ?",
                    (account_id, limit),
                ).fetchall()
            finally:
                conn.close()
            for receipt in receipts:
                payload = _parse_json(receipt["result_json"], {})
                if isinstance(payload, dict):
                    days_value = payload.get("days")
                    days = days_value if isinstance(days_value, list) else [payload]
                    values.extend(x for x in days if isinstance(x, dict) and x.get("signal"))
        return _response(
            strategy_id=strategy_id,
            detail_type=detail_type,
            values=values[:limit],
            count=len(values),
            state=(
                "V2_CURRENT_PAPER_LEDGER_DETAIL"
                if current_generation
                else "V2_HISTORICAL_PAPER_LEDGER_DETAIL"
            ),
            data_profile=account.get("data_profile"),
            execution_allowed=current_generation,
            real_orders=False,
        )
    item = _legacy_or(None, lambda: _legacy().paper_detail(strategy_id))
    if item is None:
        raise HTTPException(404, detail={"state": "PAPER_STRATEGY_NOT_FOUND", "strategy_id": strategy_id})
    values = item.get(
        {
            "nav": "nav_series",
            "positions": "positions",
            "signals": "signals",
            "orders": "orders",
            "fills": "fills",
            "ledger": "ledger",
        }.get(detail_type, detail_type),
        [],
    )
    if not isinstance(values, list):
        values = []
    return _response(
        strategy_id=strategy_id,
        detail_type=detail_type,
        values=values[:limit],
        count=len(values),
        state="LEGACY_READONLY_PAPER_DETAIL",
        execution_allowed=False,
    )


@router.get("/events")
def events(limit: int = Query(50, le=500), kind: str = Query("")):
    actual_limit = limit if isinstance(limit, int) else 50
    actual_kind = kind if isinstance(kind, str) else ""
    legacy = _legacy_or(None, lambda: _legacy().events(actual_limit, actual_kind))
    if legacy is not None:
        return _response(
            **{
                **legacy,
                "state": "LEGACY_HISTORY_AVAILABLE_VENDOR_PENDING",
                "live_vendor": "PENDING_VENDOR",
            },
            message="Legacy events are visible as historical evidence; verified live announcement vendor remains pending.",
        )
    return _response(
        events=[],
        count=0,
        kind_stats=[],
        state="PENDING_VENDOR",
        event_engine_ready=True,
        live_vendor="PENDING_VENDOR",
        message="EventStore/PIT/API/UI are ready; no verified real announcement vendor is available.",
    )


@router.get("/events/{event_id}")
def event_detail(event_id: str):
    legacy = _legacy_or(None, lambda: _legacy().events(500))
    if legacy is not None:
        item = next(
            (x for x in legacy.get("events", []) if x.get("event_id") == event_id),
            None,
        )
        if item is not None:
            return _response(event=item, state="LEGACY_EVENT_DETAIL")
    raise HTTPException(404, detail={"state": "EVENT_NOT_FOUND", "event_id": event_id})


@router.get("/data-health")
def data_health():
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        published_rows = [
            dict(x)
            for x in conn.execute(
                "SELECT p.dataset,p.manifest_id,p.manifest_hash,p.published_at,"
                "m.version,m.provenance,m.readiness,m.manifest_json,m.created_at "
                "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                "ORDER BY p.dataset"
            )
        ]
        manifest_count, manifest_rows = conn.execute(
            "SELECT count(*),coalesce(sum(json_extract(manifest_json,'$.row_count')),0) "
            "FROM data_manifests WHERE layer='CURATED' AND provenance='REAL' AND readiness='REAL_READY'"
        ).fetchone()
    finally:
        conn.close()
    coverage = {}
    watermarks = []
    for item in published_rows:
        manifest = json.loads(str(item["manifest_json"]))
        end_date = manifest.get("end_date")
        freshness = (
            "NO_DATE_BOUND"
            if end_date in (None, "None", "")
            else "HISTORICAL_SNAPSHOT"
        )
        record = {
            "dataset": item["dataset"],
            "status": item["readiness"],
            "source": item["provenance"],
            "source_tag": item["provenance"],
            "coverage": {
                "start": manifest.get("start_date"),
                "end": end_date,
            },
            "total_rows": int(manifest.get("row_count", 0) or 0),
            "row_count": int(manifest.get("row_count", 0) or 0),
            "earliest_date": manifest.get("start_date"),
            "latest_date": end_date,
            "unique_codes": manifest.get("symbol_count"),
            "quality_score": 100.0
            if manifest.get("quality_status") == "PASS"
            else 0.0,
            "manifest_hash": item["manifest_hash"],
            "manifest_id": item["manifest_id"],
            "version": item["version"],
            "freshness": freshness,
            "available_at_policy": manifest.get("available_at_policy"),
            "published_at": item["published_at"],
        }
        coverage[str(item["dataset"])] = record
        watermarks.append(
            {
                "dataset": item["dataset"],
                "latest_date": end_date,
                "published_at": item["published_at"],
                "freshness": freshness,
            }
        )
    legacy = _legacy_or(
        {"coverage": {}, "catalog": [], "overall_source": "UNAVAILABLE"},
        lambda: _legacy().data_health(),
    )
    legacy_coverage = legacy.get("coverage", {})
    combined_coverage = {
        **coverage,
        **{
            f"legacy.{key}": value
            for key, value in legacy_coverage.items()
            if key not in coverage
        },
    }
    return _response(
        overall_source="REAL_CANONICAL",
        provenance_status="PER_MANIFEST",
        manifest_count=int(manifest_count or 0),
        real_ready_manifests=int(manifest_count or 0),
        real_ready_rows=int(manifest_rows or 0),
        published_dataset_count=len(published_rows),
        coverage=combined_coverage,
        canonical_coverage=coverage,
        legacy_coverage=legacy_coverage,
        legacy_catalog=legacy.get("catalog", []),
        full_scope_status="PARTIAL",
        publication_policy={
            "legacy": "LEGACY_EVIDENCE",
            "canonical": "REAL_READY_ONLY",
            "auto_promotion": False,
        },
        watermarks=watermarks,
        recent_ingestions=len(published_rows),
        recent_errors=[],
        legacy_source=legacy.get("overall_source"),
        message="Canonical V2 publication and Legacy read-only evidence are displayed separately.",
    )


@router.get("/data/catalog")
def data_catalog():
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = [
            dict(item)
            for item in conn.execute(
                "SELECT p.dataset,p.manifest_id,p.manifest_hash,p.published_at,"
                "m.version,m.provenance,m.readiness,m.manifest_json,m.created_at "
                "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                "ORDER BY p.dataset"
            )
        ]
    finally:
        conn.close()
    for row in rows:
        row["manifest"] = json.loads(str(row.pop("manifest_json")))
        row["source_kind"] = "V2_CANONICAL"
    catalog_datasets, catalog_databases = _product_catalog_datasets(cfg)
    legacy = (
        {"datasets": catalog_datasets, "databases": catalog_databases}
        if catalog_datasets
        else _legacy_or({"datasets": [], "databases": []}, lambda: _legacy().catalog())
    )
    return _response(
        datasets=rows
        + [{**item, "source_kind": LEGACY_PROVENANCE} for item in legacy.get("datasets", [])],
        canonical_datasets=rows,
        legacy_datasets=legacy.get("datasets", []),
        legacy_databases=legacy.get("databases", []),
        product_catalog=_product_catalog_summary(cfg),
        state="CANONICAL_AND_LEGACY_CATALOG",
        lazy_legacy=True,
    )


@router.get("/data/sample")
def data_sample(
    dataset: str = Query(""),
    ts_code: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(50, le=500),
):
    if ts_code:
        daily = market_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        daily_rows = daily.get("rows", []) if isinstance(daily.get("rows"), list) else []
        if daily_rows:
            selected = daily_rows[-limit:]
            provenance = str(daily.get("provenance") or "UNKNOWN")
            return _response(
                dataset=dataset or daily.get("dataset") or "market.daily",
                sample=selected,
                count=len(selected),
                publication=daily.get("publication"),
                source={
                    "datasets_used": daily.get("datasets_used", []),
                    "manifest_hashes": daily.get("manifest_hashes", {}),
                    "price_basis": daily.get("price_basis"),
                    "realtime_semantics": False,
                },
                provenance=provenance,
                source_kind=("V2_CANONICAL" if provenance == "REAL" else LEGACY_PROVENANCE),
                state=daily.get("state"),
            )
    return _response(
        dataset=dataset,
        sample=[],
        count=0,
        state="NO_SAMPLE_QUERY",
        message="Provide ts_code to inspect a bounded sample without copying Legacy rows.",
    )


@router.get("/data/publications")
def data_publications():
    canonical = data_health()
    return _response(
        canonical_publications=canonical.get("canonical_coverage", {}),
        legacy_publications=canonical.get("legacy_coverage", {}),
        state="PUBLICATION_HISTORY_SPLIT_BY_PROVENANCE",
    )


@router.get("/system")
def system(
    include_history: bool = False,
    include_full_market: bool = False,
    include_disabled: bool = False,
):
    cfg = load_v2_config()
    current_scope = _current_etf_scope(cfg)
    realtime_evidence = _read_evidence(cfg, "REAL_OPERATION/realtime_feed_check.json") or {}
    daily_truth = daily_status(cfg)
    checked_at = datetime.now(UTC)
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        all_jobs = [
            dict(x)
            for x in conn.execute(
                "SELECT job_key,job_type AS job,business_date,status,attempt_count,fencing_token,lease_owner,lease_expires_at,updated_at AS at FROM runtime_jobs ORDER BY updated_at DESC LIMIT 50"
            )
        ]
        all_operations = [
            _enrich_operation(dict(x), checked_at=checked_at)
            for x in conn.execute(
                "SELECT component,status,detail,evidence_ref,updated_at FROM operational_status ORDER BY component"
            )
        ]
        workers = [
            dict(x)
            for x in conn.execute(
                "SELECT worker_id,worker_type,status,detail,heartbeat_at FROM worker_heartbeats ORDER BY worker_id"
            )
        ]
        runtime_counts = _runtime_counts(conn)
        current_runtime_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT status,count(*) FROM runtime_jobs WHERE "
                "(loop_kind='DAILY' AND business_date=? AND input_snapshot=?) OR "
                "(loop_kind='RESEARCH' AND business_date=?) GROUP BY status ORDER BY status",
                (
                    daily_truth["business_date"] or "",
                    daily_truth["snapshot"],
                    daily_truth["business_date"] or "",
                ),
            )
        }
    finally:
        conn.close()
    operations = [
        row
        for row in all_operations
        if (include_history or row["scope"] != "HISTORICAL_AUDIT")
        and (include_full_market or row["scope"] != "FULL_MARKET_OPTIONAL")
        and (
            include_disabled
            or str(row["status"]).upper() not in {"DISABLED", "NOT_ENABLED"}
        )
    ]
    jobs = all_jobs if include_history else [
        row
        for row in all_jobs
        if str(row.get("business_date")) == str(daily_truth["business_date"] or "")
    ]
    profile = _profile_response(cfg)
    supported_daily = _read_evidence(cfg, "SUPPORTED_PROFILE/supported_daily.json")
    schedule = _read_evidence(cfg, "SUPPORTED_PROFILE/scheduler_status.json")
    legacy = _legacy_or(
        {"status": "UNAVAILABLE", "checks": []}, lambda: _legacy().readonly_check()
    )
    legacy_dataset_count = _legacy_or(0, lambda: _legacy().catalog_dataset_count())
    ai_config, ai_status = _ai_runtime()
    ai_budget = AIService(config=ai_config).budget_status()
    live_feed_status = str(
        realtime_evidence.get("status")
        or next(
            (item["status"] for item in operations if item["component"] == "intraday_live"),
            "PENDING_VENDOR",
        )
    )
    return _response(
        project="Integrated V2",
        version="2",
        engine="quantcore",
        system_mode="REAL HISTORICAL REPLAY",
        local_only=True,
        live_trading=False,
        real_order_enabled=False,
        broker_access=False,
        account_access=False,
        broker_auth=False,
        recent_runs=jobs,
        persisted_job_count=sum(runtime_counts.values()),
        runtime_status_counts=current_runtime_counts,
        runtime_status_counts_all_history=runtime_counts,
        runtime_scope={
            "business_date": daily_truth["business_date"],
            "snapshot": daily_truth["snapshot"],
            "checked_at": checked_at.isoformat(),
            "default_view": "CURRENT_ACTIONABLE",
        },
        operational_status=operations,
        operational_status_all_count=len(all_operations),
        operational_filters={
            "include_history": include_history,
            "include_full_market": include_full_market,
            "include_disabled": include_disabled,
        },
        workers=workers,
        current_business_date=daily_truth["business_date"],
        latest_complete_market_date=daily_truth["latest_complete_market_date"],
        current_data_scope=current_scope,
        daily_operation=daily_truth,
        full_stack_health=_stack_health(cfg),
        supported_profile={
            "profile_id": profile["profile_id"],
            "status": profile["validation"]["status"],
            "securities": profile["supported_securities"],
        },
        supported_daily=supported_daily,
        scheduler=schedule,
        watch_capabilities=[
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
        watch_engine_ready=True,
        live_watch_feed=live_feed_status,
        realtime_feed_evidence={
            "business_date": realtime_evidence.get("business_date"),
            "scope": realtime_evidence.get("scope"),
            "run_id": realtime_evidence.get("run_id"),
            "request_performed": realtime_evidence.get("request_performed", False),
        },
        ai_layer_ready=True,
        ai_provider=ai_config.provider,
        ai_model=ai_config.model,
        deepseek=ai_status,
        ai_budget=ai_budget,
        ai_persistence="state/v2/ai_budget.json",
        legacy_readonly=legacy,
        legacy_dataset_count=legacy_dataset_count,
        legacy_write_policy="READ_ONLY_FOREVER",
    )


def _short_term_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"short-term record not found: {exc}")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail=f"short-term workbench unavailable: {exc}")


@router.get("/short-term/overview")
def short_term_overview():
    try:
        return _response(**ShortTermWorkbench().overview())
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/short-term/holdings")
def short_term_holdings():
    try:
        return _response(holdings=ShortTermWorkbench().holdings(), source="MANUAL_ONLY", paper_imported=False)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/short-term/plans")
def short_term_plans(plan_type: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)):
    if plan_type and plan_type not in {"CANDIDATE", "HOLDING", "BASE_T"}:
        raise HTTPException(status_code=422, detail="unknown plan_type")
    try:
        return _response(plans=ShortTermWorkbench().plans(plan_type=plan_type, limit=limit))
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/short-term/plans/{plan_id}")
def short_term_plan(plan_id: str):
    try:
        plans = [item for item in ShortTermWorkbench().plans(limit=500) if item["plan_id"] == plan_id]
        if not plans:
            raise KeyError(plan_id)
        return _response(plan=plans[0])
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/short-term/candidates")
def short_term_candidates():
    try:
        workbench = ShortTermWorkbench()
        overview_value = workbench.overview()
        latest = overview_value.get("latest_scan")
        return _response(scan=None if latest is None else workbench.scan_detail(latest["scan_id"]))
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/short-term/research")
def short_term_research():
    cfg = load_v2_config()
    evidence = _read_evidence(cfg, "SHORT_TERM/research_validation.json")
    return _response(research=evidence or {"status": "NOT_RUN", "families": [], "reason": "KNOWN-only validation has not completed"})


@router.post("/actions/short-term/holding")
def short_term_save_holding(request: ManualHoldingRequest):
    try:
        payload = request.model_dump()
        if not payload.get("as_of"):
            payload.pop("as_of", None)
        return _response(status="SAVED", holding=ShortTermWorkbench().save_holding(payload), paper_mutated=False, orders_created=0)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.post("/actions/short-term/import-holdings")
def short_term_import_holdings(request: HoldingCsvRequest):
    try:
        rows = list(csv.DictReader(io.StringIO(request.csv_text)))
        if len(rows) > 500:
            raise ValueError("CSV is limited to 500 rows")
        normalized = []
        for row in rows:
            value = ManualHoldingRequest(
                asset_id=row.get("asset_id", ""),
                shares=int(row.get("shares", "0")),
                sellable_shares=int(row.get("sellable_shares", "-1")),
                today_bought_shares=int(row.get("today_bought_shares", "0") or 0),
                cost_price=float(row.get("cost_price", "0")),
                cash_budget=float(row["cash_budget"]) if row.get("cash_budget") else None,
                risk_profile=row.get("risk_profile") or "BALANCED",
                as_of=row.get("as_of") or "",
                active=str(row.get("active", "true")).lower() not in {"0", "false", "no"},
            ).model_dump()
            normalized.append(value)
        if request.preview_only:
            return _response(status="PREVIEW", rows=normalized, persisted=0, isolated_sample=True)
        workbench = ShortTermWorkbench()
        saved = [workbench.save_holding({key: value for key, value in row.items() if value != ""}, source="CSV") for row in normalized]
        return _response(status="SAVED", rows=saved, persisted=len(saved), paper_mutated=False)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.post("/actions/short-term/scan")
def short_term_scan(request: ShortTermRunRequest):
    try:
        result = ShortTermWorkbench().scan(request.target_session or None)
        return _response(status=result["status"], scan=result, orders_created=0, live_trading=False)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.post("/actions/short-term/generate-holding-plans")
def short_term_generate_holding_plans(request: ShortTermRunRequest):
    try:
        return _response(status="COMPLETED", result=ShortTermWorkbench().generate_holding_plans(request.target_session or None), orders_created=0)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.post("/actions/short-term/settle")
def short_term_settle(request: ShortTermRunRequest):
    try:
        result = ShortTermWorkbench().settle(request.target_session)
        return _response(**result)
    except Exception as exc:
        raise _short_term_error(exc) from exc


@router.get("/watch")
def watch(limit: int = Query(100, le=1000), include_history: bool = False):
    actual_limit = limit if isinstance(limit, int) else 100
    cfg = load_v2_config()
    realtime_evidence = _read_evidence(cfg, "REAL_OPERATION/realtime_feed_check.json") or {}
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        ticks = [
            dict(x)
            for x in conn.execute(
                "SELECT source_id,asset_id,observed_at,available_at,price,volume,provenance,raw_object_hash FROM intraday_ticks ORDER BY observed_at DESC LIMIT ?",
                (actual_limit,),
            )
        ]
        raw_alerts = [
            dict(x)
            for x in conn.execute(
                "SELECT alert_id,asset_id,observed_at,alert_type,severity,state,evidence_ref,created_at FROM watch_alerts ORDER BY created_at DESC LIMIT ?",
                (actual_limit,),
            )
        ]
        state = conn.execute(
            "SELECT status,detail,updated_at FROM operational_status WHERE component='intraday_live'"
        ).fetchone()
        monitored_plans = [
            dict(x)
            for x in conn.execute(
                "SELECT p.plan_id,p.plan_type,p.asset_id,p.strategy_version,p.target_session,p.status "
                "FROM short_term_plans p WHERE p.target_session >= ? "
                "AND NOT EXISTS (SELECT 1 FROM short_term_plan_events e WHERE e.plan_id=p.plan_id AND e.event_type='INVALIDATED') "
                "AND (p.plan_type<>'CANDIDATE' OR EXISTS (SELECT 1 FROM short_term_candidates c "
                "WHERE c.plan_id=p.plan_id AND c.scan_id=(SELECT scan_id FROM short_term_scan_runs ORDER BY created_at DESC LIMIT 1))) "
                "ORDER BY p.created_at DESC LIMIT ?",
                (datetime.now().astimezone().strftime("%Y%m%d"), actual_limit),
            )
        ]
    finally:
        conn.close()
    replay = _read_evidence(cfg, "SUPPORTED_PROFILE/watch_replay.json")
    realtime_evidence = _read_evidence(cfg, "REAL_OPERATION/realtime_feed_check.json") or {}
    resolved_business_date = current_business_date(cfg)
    live_feed_status = str(
        realtime_evidence.get("status")
        or (state[0] if state else "PENDING_VENDOR")
    )
    now = datetime.now(UTC)
    alerts = []
    for item in raw_alerts:
        observed = _parse_utc_timestamp(item.get("observed_at"))
        is_current = bool(observed and (now - observed).total_seconds() <= 86400)
        if not include_history and not is_current:
            continue
        severity = str(item.get("severity") or "INFO").upper()
        item.update(
            {
                "current": is_current,
                "scope": "CURRENT_WATCH" if is_current else "HISTORICAL_WATCH",
                "reason": str(item.get("alert_type") or "WATCH_ALERT"),
                "impact": "Review the affected instrument; no order is generated by Watch.",
                "next_action": "Refresh the feed and inspect the linked evidence before acting.",
                "actionable": is_current and severity in {"WARN", "CRITICAL", "ERROR"},
            }
        )
        alerts.append(item)
    return _response(
        ticks=ticks,
        alerts=alerts,
        alert_view="ALL_HISTORY" if include_history else "CURRENT_ACTIONABLE_SCOPE",
        capabilities=[
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
        engine_ready=True,
        live_feed_status=live_feed_status,
        market_session=realtime_evidence.get("market_session"),
        market_phase=(
            realtime_evidence.get("session")
            or ("OFF_SESSION" if realtime_evidence.get("market_session") is False else "UNKNOWN")
        ),
        historical_replay=replay
        or {
            "status": "READY_TO_RUN",
            "mode": "REAL_HISTORICAL_DAILY_BAR_REPLAY",
            "realtime_semantics": False,
            "message": "No REAL minute/tick publication is available; daily bars are never labeled as ticks.",
        },
        realtime={
            "status": live_feed_status,
            "detail": realtime_evidence.get("reason") or (state[1] if state else "not initialized"),
            "updated_at": realtime_evidence.get("completed_at") or (state[2] if state else None),
            "business_date": realtime_evidence.get("business_date"),
            "scope": realtime_evidence.get("scope"),
            "run_id": realtime_evidence.get("run_id"),
            "request_performed": realtime_evidence.get("request_performed", False),
            "validated_sample_count": realtime_evidence.get("validated_sample_count", 0),
            "full_acceptance": bool(realtime_evidence.get("full_acceptance", False)),
            "continuity_window_minutes_required": 30,
            "continuity_window_verified": bool((realtime_evidence.get("continuity") or {}).get("passed", False)),
            "missing_phase_windows": realtime_evidence.get("missing_phase_windows", []),
            "recovery_verified": bool(realtime_evidence.get("recovery_verified", False)),
            "live_plan_triggers_enabled": bool(realtime_evidence.get("full_acceptance", False)),
        },
        monitored_plans=monitored_plans,
        monitored_scope="MANUAL_HOLDINGS_AND_CURRENT_CANDIDATES_ONLY",
        current_business_date=resolved_business_date.isoformat() if resolved_business_date else None,
        message=realtime_evidence.get("reason") or (state[1] if state else None),
        live_trading=False,
    )


@router.get("/stage10")
def stage10():
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT result_id,research_run_id,input_hash,formal_hash,reference_hash,status,"
            "evidence_valid,created_at FROM stage10_runs ORDER BY created_at DESC"
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["differences"] = int(
                conn.execute(
                    "SELECT count(*) FROM stage10_differences WHERE result_id=?",
                    (row["result_id"],),
                ).fetchone()[0]
            )
            item["exact_match"] = row["formal_hash"] == row["reference_hash"]
            results.append(item)
    finally:
        conn.close()
    return _response(
        state="REAL_STAGE10_EVIDENCE" if results else "NO_STAGE10_RESULT",
        results=results,
        latest=results[0] if results else None,
        exact_match=bool(results and results[0]["exact_match"]),
        differences=int(results[0]["differences"]) if results else 0,
    )


@router.get("/supported-daily")
def supported_daily():
    cfg = load_v2_config()
    evidence = _read_evidence(cfg, "SUPPORTED_PROFILE/supported_daily.json")
    return _response(
        state=("PASS" if evidence and evidence.get("status") == "PASS" else "PENDING"),
        profile_id="BOUNDED_REAL_OPERATIONAL_PROFILE_V1",
        evidence=evidence,
    )


@router.get("/metrics/{entity_type}/{entity_id}")
def metrics(entity_type: str, entity_id: str):
    meta = _meta()
    if meta["publication_id"] == "NO_PUBLICATION":
        return {**meta, "metrics": []}
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        rows = [
            dict(x)
            for x in conn.execute(
                "SELECT metric_id,value,display_value,unit,convention,metric_version AS version,source_hash,evidence_ref FROM canonical_metrics WHERE publication_id=? AND entity_type=? AND entity_id=? ORDER BY metric_id",
                (meta["publication_id"], entity_type, entity_id),
            )
        ]
    finally:
        conn.close()
    return {**meta, "metrics": rows}


@router.get("/evidence/{publication_id}")
def evidence(publication_id: str):
    cfg = load_v2_config()
    conn = _connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT * FROM reporting_publications WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "publication evidence not found")
    return _response(publication=dict(row))


@router.post("/actions/run-backtest")
def run_backtest_action(request: BacktestRunRequest):
    """Run one bounded, real-data V2 strategy backtest."""

    strategy_id = CURRENT_STRATEGY_VERSIONS.get(request.strategy_id, request.strategy_id)
    if strategy_id == "etf_rotation":
        strategy_id = "etf_rotation.v1"
    normalized = {
        "strategy_id": strategy_id,
        "profile_id": request.profile_id,
        "start_date": request.start_date or "20190626",
        "end_date": request.end_date or "20241231",
        "initial_cash": str(Decimal(str(request.initial_cash))),
    }
    request_hash = _request_hash("run-backtest", normalized)
    if strategy_id not in CURRENT_STRATEGIES:
        return _action_response(
            "run-backtest",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=request.strategy_id,
            reason="The requested strategy is not a current V2 executable strategy. Legacy results remain read-only evidence.",
            legacy_policy="LEGACY_BACKTESTS_ARE_READONLY_EVIDENCE",
        )
    if request.profile_id != "ETF3_BOUNDED_V1":
        return _action_response(
            "run-backtest",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=strategy_id,
            profile_id=request.profile_id,
            reason="Only ETF3_BOUNDED_V1 currently has complete historical prices, execution limits, Formal and independent Reference support.",
        )
    if bool(request.start_date) != bool(request.end_date):
        return _action_response(
            "run-backtest",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=strategy_id,
            reason="start_date and end_date must be provided together.",
        )
    range_reason = _backtest_range_reason(
        str(normalized["start_date"]), str(normalized["end_date"])
    )
    if range_reason:
        prior_action = _read_action("run-backtest", request.idempotency_key)
        if prior_action is not None:
            return _response(
                action="run-backtest",
                idempotency_key=request.idempotency_key,
                status="BLOCKED_DATA",
                request_hash=request_hash,
                strategy_id=strategy_id,
                profile_id=request.profile_id,
                reason=range_reason,
                required_range={"start": FROZEN_BACKTEST_RANGE[0], "end": FROZEN_BACKTEST_RANGE[1]},
                prior_action_retained_as_audit=True,
                prior_action_status=prior_action.get("status"),
                prior_evidence_ref=prior_action.get("evidence_ref"),
            )
        return _action_response(
            "run-backtest",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=strategy_id,
            profile_id=request.profile_id,
            reason=range_reason,
            required_range={"start": FROZEN_BACKTEST_RANGE[0], "end": FROZEN_BACKTEST_RANGE[1]},
        )
    cached = _cached_action("run-backtest", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    try:
        from src.quantcore.research.price_executor import run_direct_backtest

        cfg = load_v2_config(create_dirs=True)
        evidence = run_direct_backtest(
            cfg,
            strategy_id,
            normalized["start_date"],
            normalized["end_date"],
            Decimal(normalized["initial_cash"]),
        )
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        return _action_response(
            "run-backtest",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=strategy_id,
            reason=str(exc),
            legacy_write_attempted=False,
        )
    return _action_response(
        "run-backtest",
        request.idempotency_key,
        status="SUCCEEDED" if evidence.get("status") == "SUCCEEDED" else "FAILED",
        request_hash=request_hash,
        strategy_id=strategy_id,
        profile_id=request.profile_id,
        run_id=evidence.get("research_run_id"),
        backtest_id=f"v2-run-{evidence.get('research_run_id')}",
        result=evidence,
        evidence_ref=evidence.get("evidence_ref"),
        canonical_data=True,
        formal_engine=True,
        independent_reference=True,
        legacy_write_attempted=False,
    )


@router.post("/actions/run-research")
def run_research_action(request: ResearchActionRequest):
    cfg = load_v2_config(create_dirs=True)
    normalized = {"queue_item_id": request.queue_item_id or "AUTO_READY"}
    request_hash = _request_hash("run-research", normalized)
    cached = _cached_action("run-research", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    try:
        if request.queue_item_id:
            row = _queue_row(cfg, request.queue_item_id)
            if not _is_executable_queue(row):
                return _action_response(
                    "run-research",
                    request.idempotency_key,
                    status="BLOCKED_DATA",
                    request_hash=request_hash,
                    queue_item_id=request.queue_item_id,
                    reason="The requested queue item is Legacy/read-only or has no current V2 executor.",
                    legacy_write_attempted=False,
                )
            assert row is not None
            from src.quantcore.research.price_executor import run_registered_queue_item

            execution = run_registered_queue_item(cfg, request.queue_item_id)
        else:
            from src.quantcore.research.runner import execute_ready

            execution = execute_ready(cfg)
        status = "SUCCEEDED" if execution.get("status") in {"ALREADY_SUCCEEDED", "SUCCEEDED"} else "BLOCKED_DATA"
        reason = None
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        execution = {}
        status = "FAILED"
        reason = str(exc)
    return _action_response(
        "run-research",
        request.idempotency_key,
        status=status,
        request_hash=request_hash,
        queue_item_id=request.queue_item_id,
        execution=execution,
        reason=reason,
        evidence_ref=execution.get("evidence_ref") if isinstance(execution, dict) else None,
        legacy_write_attempted=False,
    )


@router.post("/actions/evaluate-strategy")
def evaluate_strategy_action(request: EvaluationActionRequest):
    cfg = load_v2_config(create_dirs=True)
    strategy_id = request.strategy_id or ""
    item = next(
        (
            value
            for value in _v2_strategy_rows(cfg)
            if strategy_id in {value.get("strategy_id"), value.get("strategy_key")}
        ),
        None,
    )
    normalized = {
        "strategy_id": strategy_id,
        "start_date": request.start_date,
        "end_date": request.end_date,
    }
    request_hash = _request_hash("evaluate-strategy", normalized)
    cached = _cached_action("evaluate-strategy", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    if item is None or not item.get("executable"):
        return _action_response(
            "evaluate-strategy",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            strategy_id=strategy_id,
            reason="A Legacy or non-executable strategy must be reimplemented under a current V2 protocol before evaluation.",
            legacy_policy="LEGACY_RESEARCH_ONLY",
            force_allowed=False,
            paper_qualification_unchanged=True,
        )
    try:
        from src.quantcore.research.price_executor import run_registered_queue_item

        execution = run_registered_queue_item(cfg, str(item["queue_item_id"]))
        status = "SUCCEEDED" if execution.get("status") in {"SUCCEEDED", "ALREADY_SUCCEEDED"} else "FAILED"
        reason = None
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        execution = {}
        status = "BLOCKED_DATA"
        reason = str(exc)
    return _action_response(
        "evaluate-strategy",
        request.idempotency_key,
        status=status,
        request_hash=request_hash,
        strategy_id=strategy_id,
        queue_item_id=item["queue_item_id"],
        execution=execution,
        reason=reason,
        force_allowed=False,
        paper_qualification_unchanged=not bool(item.get("paper_eligible")),
        evidence_ref=execution.get("evidence_ref") if isinstance(execution, dict) else None,
    )


@router.post("/actions/evaluate-factor")
def evaluate_factor_action(request: EvaluationActionRequest):
    cfg = load_v2_config(create_dirs=True)
    factor_id = request.factor_id or ""
    factor_items = _v2_factor_items(cfg, 500)
    item = next(
        (value for value in factor_items if factor_id == value.get("factor_version_id")),
        None,
    )
    if item is None:
        item = next(
            (
                value
                for value in factor_items
                if factor_id == value.get("base_factor_id") and value.get("executable")
            ),
            None,
        )
    request_hash = _request_hash(
        "evaluate-factor",
        {
            "factor_id": factor_id,
            "start_date": request.start_date,
            "end_date": request.end_date,
        },
    )
    cached = _cached_action("evaluate-factor", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    if item is None or not item.get("executable"):
        return _action_response(
            "evaluate-factor",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            factor_id=factor_id,
            reason="A Legacy or superseded factor is audit evidence; only the current registered V2 version can be evaluated.",
            legacy_policy="LEGACY_RESEARCH_ONLY",
            force_allowed=False,
        )
    if not item.get("dependency_ready", True):
        reasons = item.get("block_reasons") or ["factor data dependency is unavailable"]
        return _action_response(
            "evaluate-factor",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            factor_id=factor_id,
            reason=str(reasons[0]),
            dependency_ready=False,
            force_allowed=False,
        )
    try:
        from src.quantcore.factors.evaluator import evaluate_price_factor

        evaluation = evaluate_price_factor(cfg, str(item["factor_version_id"]))
        status = "SUCCEEDED" if evaluation.get("status") in {"COMPLETED", "INSUFFICIENT_DATA", "REJECT"} else "BLOCKED_DATA"
        reason = None
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        evaluation = {}
        status = "FAILED"
        reason = str(exc)
    return _action_response(
        "evaluate-factor",
        request.idempotency_key,
        status=status,
        request_hash=request_hash,
        factor_id=factor_id,
        evaluation=evaluation,
        reason=reason,
        evidence_ref=evaluation.get("evidence_ref") if isinstance(evaluation, dict) else None,
        force_allowed=False,
    )


@router.post("/actions/run-queue")
def run_queue_action(request: ResearchActionRequest):
    cfg = load_v2_config(create_dirs=True)
    normalized = {"queue_item_id": request.queue_item_id or "AUTO_READY"}
    request_hash = _request_hash("run-queue", normalized)
    cached = _cached_action("run-queue", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    try:
        if request.queue_item_id:
            row = _queue_row(cfg, request.queue_item_id)
            if not _is_executable_queue(row):
                return _action_response(
                    "run-queue",
                    request.idempotency_key,
                    status="BLOCKED_DATA",
                    request_hash=request_hash,
                    queue_item_id=request.queue_item_id,
                    reason="The requested queue item is Legacy/read-only or has no current V2 executor.",
                )
            from src.quantcore.research.price_executor import run_registered_queue_item

            execution = run_registered_queue_item(cfg, request.queue_item_id)
        else:
            from src.quantcore.research.runner import execute_ready

            execution = execute_ready(cfg)
        status = "SUCCEEDED" if execution.get("status") in {"ALREADY_SUCCEEDED", "SUCCEEDED"} else "BLOCKED_DATA"
        reason = None
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        execution = {}
        status = "FAILED"
        reason = str(exc)
    return _action_response(
        "run-queue",
        request.idempotency_key,
        status=status,
        request_hash=request_hash,
        queue_item_id=request.queue_item_id or "queue-ctrl-v1",
        execution=execution,
        reason=reason,
        force_allowed=False,
        evidence_ref=execution.get("evidence_ref") if isinstance(execution, dict) else None,
    )


@router.post("/actions/queue/{queue_item_id}")
def queue_action(queue_item_id: str, request: QueueActionRequest):
    cfg = load_v2_config(create_dirs=True)
    row = _queue_row(cfg, queue_item_id)
    request_hash = _request_hash(
        "queue-action",
        {"queue_item_id": queue_item_id, "action": request.action},
    )
    cached = _cached_action("queue-action", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    if not _is_executable_queue(row):
        return _action_response(
            "queue-action",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            queue_item_id=queue_item_id,
            requested_action=request.action,
            reason="Queue item is Legacy/read-only or has no current V2 executor.",
        )
    assert row is not None
    try:
        from src.quantcore.research.queue import ResearchStore
        from src.quantcore.research.registry import QueueStatus

        if request.action == "refresh":
            refreshed = _refresh_queue_state(cfg, queue_item_id)
            return _action_response(
                "queue-action",
                request.idempotency_key,
                status="SUCCEEDED",
                request_hash=request_hash,
                queue_item_id=queue_item_id,
                requested_action=request.action,
                refreshed=refreshed,
            )
        current = str(row["status"])
        if request.action == "retry":
            if current != QueueStatus.FAILED.value:
                return _action_response(
                    "queue-action",
                    request.idempotency_key,
                    status="BLOCKED_DATA",
                    request_hash=request_hash,
                    queue_item_id=queue_item_id,
                    requested_action=request.action,
                    reason=f"Retry requires FAILED; current state is {current}.",
                )
            ResearchStore(cfg.paths.state / "control.sqlite").transition(queue_item_id, QueueStatus.READY)
            current = QueueStatus.READY.value
        if request.action == "run":
            if current not in {QueueStatus.PLANNED.value, QueueStatus.BLOCKED_DATA.value, QueueStatus.READY.value, QueueStatus.SUCCEEDED.value}:
                return _action_response(
                    "queue-action",
                    request.idempotency_key,
                    status="BLOCKED_DATA",
                    request_hash=request_hash,
                    queue_item_id=queue_item_id,
                    requested_action=request.action,
                    reason=f"Queue item cannot run from state {current}.",
                )
            _refresh_queue_state(cfg, queue_item_id)
            from src.quantcore.research.price_executor import run_registered_queue_item

            execution = run_registered_queue_item(cfg, queue_item_id)
            status = "SUCCEEDED" if execution.get("status") in {"SUCCEEDED", "ALREADY_SUCCEEDED"} else "BLOCKED_DATA"
            return _action_response(
                "queue-action",
                request.idempotency_key,
                status=status,
                request_hash=request_hash,
                queue_item_id=queue_item_id,
                requested_action=request.action,
                execution=execution,
                evidence_ref=execution.get("evidence_ref"),
                reason=None if status == "SUCCEEDED" else execution.get("reason"),
            )
        if request.action != "cancel":
            raise ValueError(f"unsupported queue action: {request.action}")
        if current not in {QueueStatus.PLANNED.value, QueueStatus.BLOCKED_DATA.value, QueueStatus.READY.value}:
            return _action_response(
                "queue-action",
                request.idempotency_key,
                status="BLOCKED_DATA",
                request_hash=request_hash,
                queue_item_id=queue_item_id,
                requested_action=request.action,
                reason=f"Cannot cancel a queue item in state {current}.",
            )
        ResearchStore(cfg.paths.state / "control.sqlite").transition(queue_item_id, QueueStatus.CANCELLED)
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.DatabaseError) as exc:
        return _action_response(
            "queue-action",
            request.idempotency_key,
            status="FAILED",
            request_hash=request_hash,
            queue_item_id=queue_item_id,
            requested_action=request.action,
            reason=str(exc),
        )
    return _action_response(
        "queue-action",
        request.idempotency_key,
        status="SUCCEEDED",
        request_hash=request_hash,
        queue_item_id=queue_item_id,
        requested_action=request.action,
        reason="Future execution cancelled; no gate or OOS state was changed.",
    )


@router.post("/actions/paper")
def paper_action(request: PaperActionRequest):
    strategy_id = request.strategy_id or ""
    request_hash = _request_hash(
        "paper-action",
        {
            "strategy_id": strategy_id,
            "action": request.action,
            "trade_date": request.trade_date,
            "start_date": request.start_date,
            "end_date": request.end_date,
        },
    )
    cached = _cached_action("paper-action", request.idempotency_key, request_hash)
    if cached is not None:
        return cached
    legacy = _legacy_or(None, lambda: _legacy().paper_detail(strategy_id))
    if legacy is not None:
        return _action_response(
            "paper-action",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            requested_action=request.action,
            strategy_id=strategy_id,
            reason="Legacy PAPER is read-only and cannot be admitted, stepped, paused, resumed, or continued by V2.",
            legacy_status=LEGACY_PAPER,
            execution_allowed=False,
        )
    cfg = load_v2_config(create_dirs=True)
    current = next(
        (item for item in _v2_strategy_rows(cfg) if item.get("strategy_id") == strategy_id),
        None,
    )
    if current is None:
        return _action_response(
            "paper-action",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            requested_action=request.action,
            strategy_id=strategy_id,
            reason="No current V2 strategy version matches the request.",
            execution_allowed=False,
            real_orders=False,
        )
    if not current.get("paper_eligible") and request.action in {
        "admit",
        "create_generation",
    }:
        return _action_response(
            "paper-action",
            request.idempotency_key,
            status="BLOCKED_DATA",
            request_hash=request_hash,
            requested_action=request.action,
            strategy_id=strategy_id,
            reason="PAPER admission requires an active qualification backed by Stage09 CANDIDATE and Stage10 PASS.",
            execution_allowed=False,
            real_orders=False,
        )
    try:
        from src.quantcore.portfolio.paper_operations import operate

        result = operate(
            cfg,
            strategy_id,
            request.action,
            trade_date=request.trade_date,
            start_date=request.start_date,
            end_date=request.end_date,
            request_hash=request_hash,
        )
    except (OSError, RuntimeError, ValueError, KeyError, PermissionError, sqlite3.DatabaseError) as exc:
        result = {"status": "FAILED", "reason": str(exc)}
    status = str(result.get("status", "FAILED"))
    return _action_response(
        "paper-action",
        request.idempotency_key,
        status=status,
        request_hash=request_hash,
        requested_action=request.action,
        strategy_id=strategy_id,
        execution=result,
        state=result.get("state"),
        reason=result.get("reason"),
        execution_allowed=bool(result.get("execution_allowed", False)),
        real_orders=False,
    )


# ── AI Chat: non-sensitive context whitelist ──────────────────────────
# Only these keys may be sent to the external AI model.
# Holdings, plan counts, costs, or any private fields are NEVER included.
_AI_CONTEXT_WHITELIST_KEYS = frozenset({
    "system_status", "system_business_date", "system_data_scope",
    "system_latest_eod", "system_paper_status", "system_watch_status",
    "scan_status", "scan_data_date", "scan_target_session",
    "scan_candidate_count",
    "candidate_asset_id", "candidate_strategy", "candidate_entry_low",
    "candidate_entry_high", "candidate_target", "candidate_stop",
    "candidate_status",
    "stock_ts_code", "stock_latest_date", "stock_close",
    "stock_change", "stock_sma20", "stock_sma60",
})


def _gather_ai_context(ts_code: str | None) -> dict[str, Any]:
    """Gather ONLY non-sensitive context for AI chat.

    Privacy rules:
    - NO holding count, plan counts, costs, or private fields
    - NO raw user data from databases
    - Only system status, public scan metadata, and public market data
    """
    import re as _re

    result: dict[str, Any] = {}
    cfg = load_v2_config()

    # System status (non-sensitive summary only)
    try:
        from src.quantcore.activation.status import daily_status
        daily_truth = daily_status(cfg)
        result["system_status"] = "running"
        result["system_business_date"] = daily_truth.get("business_date")
    except Exception:
        result["system_status"] = "unavailable"

    # Scan metadata only — no holding info, no plan counts
    try:
        from src.quantcore.short_term import ShortTermWorkbench
        wb = ShortTermWorkbench(cfg)
        overview = wb.overview()
        scan = overview.get("latest_scan") or {}
        result["scan_status"] = scan.get("status")
        result["scan_data_date"] = scan.get("data_date")
        result["scan_target_session"] = scan.get("target_session")
        # Only public candidate count, NOT holding/plan counts
        candidates = scan.get("candidates") or []
        result["scan_candidate_count"] = len(candidates)
        # Public candidate summaries only — no private fields
        for i, c in enumerate(candidates[:12]):
            prefix = f"candidate_{i}"
            result[f"{prefix}_asset_id"] = c.get("asset_id")
            result[f"{prefix}_strategy"] = c.get("strategy_version")
            result[f"{prefix}_entry_low"] = c.get("entry_low")
            result[f"{prefix}_entry_high"] = c.get("entry_high")
            result[f"{prefix}_target"] = c.get("target_low")
            result[f"{prefix}_stop"] = c.get("stop_price")
            result[f"{prefix}_status"] = c.get("status")
    except Exception:
        result["scan_status"] = "unavailable"

    # Public market data for a specific stock (if requested)
    if ts_code and _re.fullmatch(r"(?:0|3|6)\d{5}\.(?:SZ|SH)", ts_code):
        try:
            daily_result = market_daily(ts_code=ts_code, start_date="20250101")
            rows = daily_result.get("rows", [])
            if rows:
                latest = rows[-1]
                result["stock_ts_code"] = ts_code
                result["stock_latest_date"] = latest.get("trade_date")
                result["stock_close"] = latest.get("close")
                if len(rows) >= 2:
                    result["stock_change"] = round(
                        float(latest.get("close", 0)) - float(rows[-2].get("close", 0)), 3
                    )
                closes = [float(r.get("close", 0)) for r in rows[-60:] if r.get("close")]
                if len(closes) >= 20:
                    result["stock_sma20"] = round(sum(closes[-20:]) / 20, 3)
                if len(closes) >= 60:
                    result["stock_sma60"] = round(sum(closes[-60:]) / 60, 3)
        except Exception:
            pass

    return result


@router.post("/ai/chat")
def ai_chat(request: AIChatRequest):
    """AI assistant chat endpoint.

    Privacy: only non-sensitive whitelist data is sent to the external model.
    Holdings, plan details, costs, and private fields are NEVER included.
    Rate-limited through AIService budget. User message is forwarded as-is
    but the system prompt instructs the model to refuse private data queries.
    """
    from src.quantcore.intelligence.ai import AIService, DeepSeekAdapter, deepseek_api_key_present

    # Check if AI is configured
    if not deepseek_api_key_present():
        return {
            "status": "NOT_CONFIGURED",
            "message": "AI 助手未配置。请在 config/v2/ai.local.json 中设置 api_key 和 enabled: true。",
            "response": None,
        }

    # Rate limiting and budget via AIService
    service = AIService()
    budget = service.budget_status()
    remaining_raw = budget.get("remaining_calls_today", 0)
    remaining = int(remaining_raw) if isinstance(remaining_raw, (int, float, str)) else 0
    if remaining <= 0:
        return {
            "status": "RATE_LIMITED",
            "message": "AI 调用次数已达今日上限，请明天再试。",
            "response": None,
        }

    # Gather ONLY whitelisted non-sensitive context
    context_data = _gather_ai_context(request.ts_code)

    # Verify all keys are in whitelist (defense in depth)
    for key in context_data:
        if key not in _AI_CONTEXT_WHITELIST_KEYS:
            # Strip any non-whitelisted keys
            context_data = {k: v for k, v in context_data.items() if k in _AI_CONTEXT_WHITELIST_KEYS}
            break

    # Build the prompt with explicit privacy instructions
    system_prompt = """你是一个量化分析助手。你可以回答关于以下方面的问题：
- 市场行情和数据状态
- 短线扫描候选（公开信息）
- 技术指标和策略信号（公开计算）
- 系统状态和数据可用性

严格禁止：
1. 不得给出投资建议或预测价格涨跌
2. 不得访问或提及用户的私人持仓、成本、资金或交易记录
3. 不得泄露任何账户信息或计划细节
4. 所有数据仅供研究参考，不构成投资建议
5. 如果数据不完整或有缺口，如实告知
6. 用中文回答
7. 如果用户询问私人信息，明确告知无法提供"""

    user_prompt = f"""用户问题：{request.message}

当前公开数据摘要：
{json.dumps(context_data, ensure_ascii=False, indent=2)}

请基于以上公开数据回答用户的问题。如果数据不足以回答，请如实说明。不要编造数据或给出投资建议。"""

    # Call AI service with timeout
    try:
        adapter = DeepSeekAdapter(timeout_seconds=30.0)
        result = adapter.explain(
            f"{system_prompt}\n\n{user_prompt}",
            max_tokens=1500,
        )

        if result.status.value == "PASS":
            # Track the call in budget
            service._day_calls += 1
            service._day_tokens += 1500
            service._persist_budget()
            return {
                "status": "OK",
                "response": result.text,
                "provider": result.provider,
                "disclaimer": "以上内容由 AI 生成，仅供研究参考，不构成投资建议。系统未发送任何私人数据。",
                "privacy_notice": "发送给 AI 的数据仅限公开行情和系统状态，不含持仓、成本或计划详情。",
            }
        else:
            return {
                "status": result.status.value,
                "message": f"AI 调用失败：{result.reason}",
                "response": None,
            }
    except Exception as exc:
        return {
            "status": "ERROR",
            "message": f"AI 服务异常：{type(exc).__name__}",
            "response": None,
        }
