"""Real, append-only execution for the currently runnable research queue.

The first executor is deliberately narrow: it runs the frozen cash CONTROL
variant over the published REAL fund_daily dataset.  It does not turn missing
execution limits, historical stock states, or financial revisions into
synthetic inputs.  Strategy families remain blocked until their dependency
assessment is satisfied.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.quantcore.contracts.config import V2Config
from src.quantcore.contracts.execution import DailySnapshot
from src.quantcore.data.manifest import ManifestStore
from src.quantcore.data.models import Readiness
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService
from src.quantcore.research.queue import ResearchStore
from src.quantcore.research.registry import (
    QueueStatus,
    ResearchProtocol,
    Stage09Result,
    StrategyVersion,
    canonical_hash,
    evaluate_stage09,
)
from src.quantcore.validation.stage10 import Stage10Store, compare_snapshots


CONTROL_QUEUE_ID = "queue-ctrl-v1"
CONTROL_DATASET = "fund_daily"
CONTROL_ASSET_SCOPE = "GLOBAL"
CONTROL_FAMILY_SCOPE = "CONTROL"
INITIAL_CASH = "1000000.00"


def _json(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _read_publication(cfg: V2Config, dataset: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    database = cfg.paths.state / "control.sqlite"
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT m.manifest_id,m.manifest_json FROM data_publications p "
            "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE p.dataset=? AND m.layer='CURATED' AND m.provenance='REAL' "
            "AND m.readiness='REAL_READY'",
            (dataset,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"missing REAL_READY publication: {dataset}")
    manifest_store = ManifestStore(
        cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256"
    )
    manifest = manifest_store.load(str(row[0]))
    if manifest.readiness is not Readiness.REAL_READY:
        raise ValueError(f"publication is not REAL_READY: {dataset}")
    records: list[dict[str, Any]] = []
    for digest in manifest.object_hashes:
        object_path = manifest_store.object_root / digest[:2] / digest
        records.extend(json.loads(object_path.read_text(encoding="utf-8")))
    return manifest.payload() | {"manifest_hash": manifest.manifest_hash}, records


def _protocol(payload: str) -> ResearchProtocol:
    raw = json.loads(payload)
    raw["parameter_grid"] = tuple((str(name), tuple(values)) for name, values in raw["parameter_grid"])
    raw["data_dependencies"] = tuple(raw["data_dependencies"])
    raw["train_window"] = tuple(raw["train_window"])
    raw["validation_windows"] = tuple(tuple(window) for window in raw["validation_windows"])
    raw["controls"] = tuple(raw["controls"])
    raw["candidate_threshold"] = Decimal(str(raw["candidate_threshold"]))
    return ResearchProtocol(**raw)


def _load_queue_item(cfg: V2Config, queue_item_id: str) -> tuple[ResearchProtocol, StrategyVersion, str]:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT q.status,q.strategy_version_id,q.protocol_hash,p.protocol_json,"
            "v.family_id,v.source_code_hash,v.snapshot_hash,v.universe_version "
            "FROM research_queue q "
            "JOIN research_protocols p ON p.protocol_hash=q.protocol_hash "
            "JOIN strategy_versions v ON v.strategy_version_id=q.strategy_version_id "
            "WHERE q.queue_item_id=?",
            (queue_item_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(queue_item_id)
    protocol = _protocol(str(row["protocol_json"]))
    version = StrategyVersion(
        str(row["strategy_version_id"]),
        str(row["family_id"]),
        str(row["protocol_hash"]),
        str(row["source_code_hash"]),
        str(row["snapshot_hash"]),
        str(row["universe_version"]),
    )
    return protocol, version, str(row["status"])


def _windows(protocol: ResearchProtocol, sessions: list[date]) -> list[dict[str, Any]]:
    declared = [("train", *protocol.train_window)] + [
        (f"validation_{index + 1}", *window)
        for index, window in enumerate(protocol.validation_windows)
    ]
    result: list[dict[str, Any]] = []
    for name, start, end in declared:
        left, right = _date(start), _date(end)
        selected = [session for session in sessions if left <= session <= right]
        result.append(
            {
                "name": name,
                "declared_start": left.isoformat(),
                "declared_end": right.isoformat(),
                "observed_start": selected[0].isoformat() if selected else None,
                "observed_end": selected[-1].isoformat() if selected else None,
                "session_count": len(selected),
            }
        )
    return result


def _cash_snapshots(sessions: list[date]) -> tuple[DailySnapshot, ...]:
    return tuple(
        DailySnapshot(
            on=session.isoformat(),
            orders=(),
            fills=(),
            cash=INITIAL_CASH,
            positions=(),
            total_fees="0.00",
            nav=INITIAL_CASH,
            event_receipts=(),
        )
        for session in sessions
    )


def _insert_research_run(
    cfg: V2Config,
    *,
    research_run_id: str,
    queue_item_id: str,
    version: StrategyVersion,
    protocol: ResearchProtocol,
    snapshot_hash: str,
    receipt_id: str,
    implementation_hash: str,
    dataset_manifest: dict[str, Any],
    windows: list[dict[str, Any]],
    metrics: dict[str, Any],
    robustness: dict[str, Any],
    evidence_ref: str,
    execution_scope: str = "CONTROL",
    execution_rules: str | None = None,
) -> None:
    values = (
        research_run_id,
        queue_item_id,
        version.strategy_version_id,
        protocol.protocol_hash,
        snapshot_hash,
        receipt_id,
        "SUCCEEDED",
        execution_scope,
        implementation_hash,
        _json(dataset_manifest),
        _json(windows),
        protocol.cost_model,
        execution_rules or "CONTROL_CASH_NO_ORDERS",
        _json(metrics),
        _json(robustness),
        evidence_ref,
    )
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT research_run_id,queue_item_id,strategy_version_id,protocol_hash,snapshot_hash,"
            "oos_receipt_id,status,execution_scope,implementation_hash,dataset_manifest_json,window_json,"
            "cost_model,execution_rules,metrics_json,robustness_json,evidence_ref "
            "FROM research_runs WHERE research_run_id=?",
            (research_run_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (datetime.now(UTC).isoformat(),),
            )
        elif tuple(existing) != values:
            raise RuntimeError("immutable research run conflict")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_stage09_evidence(
    cfg: V2Config,
    *,
    result_id: str,
    research_run_id: str,
    version: StrategyVersion,
    protocol: ResearchProtocol,
    result: Stage09Result,
    receipt_id: str,
    dataset_manifest: dict[str, Any],
    windows: list[dict[str, Any]],
    metrics: dict[str, Any],
    robustness: dict[str, Any],
    execution_rules: str = "CONTROL_CASH_NO_ORDERS",
) -> None:
    values = (
        result_id,
        research_run_id,
        version.strategy_version_id,
        protocol.protocol_hash,
        result.snapshot_hash,
        receipt_id,
        _json(dataset_manifest),
        _json(windows),
        protocol.cost_model,
        execution_rules,
        _json(metrics),
        _json(robustness),
    )
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT result_id,research_run_id,strategy_version_id,protocol_hash,snapshot_hash,"
            "oos_receipt_id,dataset_manifest_json,window_json,cost_model,execution_rules,metrics_json,"
            "robustness_json FROM stage09_evidence WHERE result_id=?",
            (result_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO stage09_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (datetime.now(UTC).isoformat(),),
            )
        elif tuple(existing) != values:
            raise RuntimeError("immutable Stage09 evidence conflict")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_component(cfg: V2Config, status: str, detail: str, evidence: str | None = None) -> None:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,detail=excluded.detail,"
            "evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            ("research_loop", status, detail, evidence, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _write_evidence(cfg: V2Config, report: dict[str, Any]) -> str:
    target = cfg.paths.artifacts / "evidence" / "S13" / "real_research_ctrl_cash.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return str(target.relative_to(cfg.paths.artifacts.parent))


def run_control_cash(cfg: V2Config, queue_item_id: str = CONTROL_QUEUE_ID) -> dict[str, Any]:
    protocol, version, status = _load_queue_item(cfg, queue_item_id)
    if not protocol.control_only or protocol.family_id != "CONTROL":
        raise ValueError("the first executor only accepts the frozen CONTROL protocol")
    if status == QueueStatus.SUCCEEDED.value:
        conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
        try:
            row = conn.execute(
                "SELECT research_run_id,evidence_ref FROM research_runs WHERE queue_item_id=? ORDER BY created_at DESC LIMIT 1",
                (queue_item_id,),
            ).fetchone()
        finally:
            conn.close()
        evidence_ref = row[1] if row else None
        _set_component(
            cfg,
            "PASS",
            "real CONTROL cash run already persisted; strategy families remain dependency-gated",
            evidence_ref,
        )
        return {
            "status": "ALREADY_SUCCEEDED",
            "queue_item_id": queue_item_id,
            "research_run_id": row[0] if row else None,
        }
    if status != QueueStatus.READY.value:
        return {"status": "BLOCKED", "queue_item_id": queue_item_id, "queue_status": status}

    price_manifest, rows = _read_publication(cfg, CONTROL_DATASET)
    assets = sorted({str(row.get("ts_code")) for row in rows if row.get("ts_code")})
    if not assets:
        raise ValueError("REAL fund_daily publication contains no assets")
    grouped: dict[date, set[str]] = {}
    for row in rows:
        if row.get("ts_code") and row.get("trade_date"):
            grouped.setdefault(_date(row["trade_date"]), set()).add(str(row["ts_code"]))
    all_dates = sorted(grouped)
    complete_sessions = [session for session in all_dates if grouped[session] >= set(assets)]
    if not complete_sessions:
        raise ValueError("REAL fund_daily publication has no complete sessions")
    windows = _windows(protocol, complete_sessions)
    validation_sessions = [
        session
        for window, _info in zip(protocol.validation_windows, windows[1:], strict=True)
        for start, end in (window,)
        for session in complete_sessions
        if _date(start) <= session <= _date(end)
    ]
    validation_blocks = sum(item["session_count"] > 0 for item in windows[1:])
    if validation_blocks < 3 or not validation_sessions:
        raise ValueError("CONTROL requires three non-empty REAL validation windows")

    dataset_manifest = {
        "dataset": CONTROL_DATASET,
        "manifest_id": price_manifest["manifest_id"],
        "manifest_hash": price_manifest["manifest_hash"],
        "provenance": price_manifest["provenance"],
        "readiness": price_manifest["readiness"],
        "row_count": price_manifest["row_count"],
        "asset_count": len(assets),
        "assets": assets,
        "observed_dates": len(all_dates),
        "complete_sessions": len(complete_sessions),
        "incomplete_sessions_excluded": len(all_dates) - len(complete_sessions),
    }
    implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    snapshot_base = {
        "protocol_hash": protocol.protocol_hash,
        "strategy_version_id": version.strategy_version_id,
        "dataset_manifest_hash": price_manifest["manifest_hash"],
        "selected_parameters": {"control": "cash"},
        "validation_sessions": [session.isoformat() for session in validation_sessions],
    }
    snapshot_hash = canonical_hash(snapshot_base)
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=protocol.protocol_hash,
        snapshot_hash=snapshot_hash,
        start=_date(protocol.train_window[0]),
        end=_date(protocol.validation_windows[-1][1]),
        purpose=AccessPurpose.RESEARCH,
        asset_scope=CONTROL_ASSET_SCOPE,
        family_scope=CONTROL_FAMILY_SCOPE,
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        raise ValueError(f"OOS authorization denied: {receipt.decision.value}")

    ResearchStore(cfg.paths.state / "control.sqlite").transition(queue_item_id, QueueStatus.RUNNING)
    formal_days = _cash_snapshots(validation_sessions)
    reference_days = _cash_snapshots(validation_sessions)
    stage10 = compare_snapshots(
        formal_days,
        reference_days,
        input_hash=snapshot_hash,
        sufficient_data=len(formal_days) == len(reference_days) and validation_blocks >= 3,
    )
    metrics = {
        "primary_metric": "0",
        "metric_name": protocol.primary_metric,
        "effective_trades": 0,
        "validation_blocks": validation_blocks,
        "variant": "cash",
        "formal_snapshot_count": len(formal_days),
        "reference_snapshot_count": len(reference_days),
    }
    robustness = {
        "control_only": True,
        "promotion_allowed": False,
        "future_factor_sentinel": "NOT_EXECUTED_IN_THIS_FROZEN_VARIANT",
        "parameter_coverage": "cash_only",
        "cost_sensitivity": "NOT_APPLICABLE_NO_ORDERS",
    }
    result = evaluate_stage09(
        protocol,
        version,
        readiness=Readiness.REAL_READY,
        access_receipt=receipt,
        effective_trades=0,
        validation_blocks=validation_blocks,
        primary_metric=Decimal(metrics["primary_metric"]),
        future_leakage=False,
        snapshot_hash=snapshot_hash,
    )
    research_run_id = f"real-control-cash-{snapshot_hash[:24]}"
    evidence_report: dict[str, Any] = {
        "status": "SUCCEEDED",
        "execution_scope": "CONTROL",
        "queue_item_id": queue_item_id,
        "research_run_id": research_run_id,
        "protocol_id": protocol.protocol_id,
        "protocol_hash": protocol.protocol_hash,
        "strategy_version_id": version.strategy_version_id,
        "snapshot_hash": snapshot_hash,
        "implementation_hash": implementation_hash,
        "selected_parameters": {"control": "cash"},
        "dataset": dataset_manifest,
        "windows": windows,
        "oos_receipt_id": receipt.receipt_id,
        "oos_decision": receipt.decision.value,
        "stage09": {
            "result_id": f"real-stage09-control-cash-{snapshot_hash[:24]}",
            "rating": result.rating.value,
            "reasons": list(result.reasons),
            "primary_metric": str(result.primary_metric) if result.primary_metric is not None else None,
            "effective_trades": result.effective_trades,
            "validation_blocks": result.validation_blocks,
        },
        "stage10": {
            "status": stage10.status.value,
            "evidence_valid": stage10.evidence_valid,
            "formal_hash": stage10.formal_hash,
            "reference_hash": stage10.reference_hash,
            "differences": len(stage10.differences),
        },
        "synthetic": False,
        "limitations": [
            "This is a real CONTROL cash variant, not a promoted strategy.",
            "Other CTRL parameter values are not silently substituted or claimed as executed.",
            "No live market-hours data, broker, account, or real order path was used.",
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    evidence_ref = _write_evidence(cfg, evidence_report)
    _insert_research_run(
        cfg,
        research_run_id=research_run_id,
        queue_item_id=queue_item_id,
        version=version,
        protocol=protocol,
        snapshot_hash=snapshot_hash,
        receipt_id=receipt.receipt_id,
        implementation_hash=implementation_hash,
        dataset_manifest=dataset_manifest,
        windows=windows,
        metrics=metrics,
        robustness=robustness,
        evidence_ref=evidence_ref,
    )
    result_id = evidence_report["stage09"]["result_id"]
    ResearchStore(cfg.paths.state / "control.sqlite").transition(queue_item_id, QueueStatus.SUCCEEDED)
    ResearchStore(cfg.paths.state / "control.sqlite").record_stage09(result_id, queue_item_id, result)
    _record_stage09_evidence(
        cfg,
        result_id=result_id,
        research_run_id=research_run_id,
        version=version,
        protocol=protocol,
        result=result,
        receipt_id=receipt.receipt_id,
        dataset_manifest=dataset_manifest,
        windows=windows,
        metrics=metrics,
        robustness=robustness,
    )
    stage10_id = Stage10Store(cfg.paths.state / "control.sqlite").record(research_run_id, stage10)
    evidence_report["stage09"]["result_id"] = result_id
    evidence_report["stage10"]["result_id"] = stage10_id
    evidence_report["evidence_ref"] = evidence_ref
    _write_evidence(cfg, evidence_report)
    _set_component(
        cfg,
        "PASS",
        "real CONTROL cash run completed; strategy families remain dependency-gated",
        evidence_ref,
    )
    return evidence_report


def execute_ready(cfg: V2Config) -> dict[str, Any]:
    """Execute one READY current-V2 item using its registered executor."""

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        rows = conn.execute(
            "SELECT q.queue_item_id,q.strategy_version_id,q.status,p.family_id "
            "FROM research_queue q JOIN research_protocols p ON p.protocol_hash=q.protocol_hash "
            "WHERE q.status IN ('READY','SUCCEEDED') ORDER BY q.priority,q.created_at"
        ).fetchall()
    finally:
        conn.close()
    supported = {
        "etf_rotation.v2",
        "etf_lowvol.v1",
        "etf_trend_defensive.v1",
    }
    for queue_item_id, strategy_version_id, status, _family in rows:
        if str(strategy_version_id) not in supported:
            continue
        from src.quantcore.research.price_executor import run_registered_queue_item

        return run_registered_queue_item(cfg, str(queue_item_id))
    protocol, _version, status = _load_queue_item(cfg, CONTROL_QUEUE_ID)
    if status in {QueueStatus.READY.value, QueueStatus.SUCCEEDED.value} and protocol.control_only:
        return run_control_cash(cfg, CONTROL_QUEUE_ID)
    return {
        "status": "NO_EXECUTABLE_READY_ITEM",
        "queue_item_id": CONTROL_QUEUE_ID,
        "queue_status": status,
        "reason": "No READY current-V2 executor is available; unsupported Legacy/declaration families remain fail-closed.",
    }
