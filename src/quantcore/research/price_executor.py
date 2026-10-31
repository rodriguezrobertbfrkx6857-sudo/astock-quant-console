"""Real bounded price-strategy research and backtest application service.

This module is deliberately narrow: it only accepts the published ETF3
price/limit snapshot and the registered price strategies.  It reuses the
existing Formal engine and the independent reference engine, but it does not
reuse the historical replay's fixed window.  A request's protocol, dates,
snapshot and configuration all participate in the run identity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reference_engine.engine import RefEngine, RefMarket, RefOrder

from src.quantcore.accounting.ledger import AccountState
from src.quantcore.contracts.config import V2Config
from src.quantcore.contracts.execution import DailySnapshot
from src.quantcore.data.manifest import ManifestStore
from src.quantcore.data.models import Readiness
from src.quantcore.execution.formal import FormalEngine, MarketState, Order
from src.quantcore.research.dependencies import assess_protocol
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService
from src.quantcore.research.queue import ResearchStore
from src.quantcore.research.registry import (
    QueueStatus,
    ResearchProtocol,
    StrategyVersion,
    canonical_hash,
    evaluate_stage09,
)
from src.quantcore.research.runner import (
    _date,
    _insert_research_run,
    _protocol,
    _record_stage09_evidence,
)
from src.quantcore.reporting.metrics import MetricCalculator, ReportingStore
from src.quantcore.rules.rulebook import default_rulebook
from src.quantcore.strategies import PricePoint, STRATEGY_CATALOG, run_strategy
from src.quantcore.universe.models import Board, SecurityType
from src.quantcore.validation.projection import formal_snapshot, reference_snapshot
from src.quantcore.validation.stage10 import Stage10Store, compare_snapshots


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRICE_DATASET = "research_etf3_prices"
LIMIT_DATASET = "research_etf3_limits"
MASTER_DATASET = "historical_etf_master"
ETF3 = ("510300.SH", "510500.SH", "159915.SZ")
SUPPORTED_STRATEGIES = frozenset({
    "etf_rotation.v1",
    "etf_lowvol.v1",
    "etf_trend_defensive.v1",
    "etf_momentum_20.v1",
    "etf_risk_adjusted_60.v1",
})
SUPPORTED_FAMILIES = frozenset({
    "ETF_ROTATION",
    "ETF_LOW_VOL",
    "ETF_TREND_DEFENSIVE",
    "ETF_MOMENTUM_SHORT",
    "ETF_RISK_ADJUSTED",
})
INITIAL_CASH = Decimal("1000000.00")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decimal(row: dict[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None:
        raise ValueError(f"missing {field}")
    return Decimal(str(value))


def _next_session_factory(sessions: list[date]):
    mapping = {left: right for left, right in zip(sessions, sessions[1:], strict=False)}

    def next_session(on: date) -> date:
        if on in mapping:
            return mapping[on]
        candidate = on + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    return next_session


def _publication(cfg: V2Config, dataset: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    database = cfg.paths.state / "control.sqlite"
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)
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
        raise RuntimeError(f"missing REAL_READY publication: {dataset}")
    store = ManifestStore(cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256")
    manifest = store.load(str(row[0]))
    records: list[dict[str, Any]] = []
    for digest in manifest.object_hashes:
        path = store.object_root / digest[:2] / digest
        records.extend(dict(item) for item in json.loads(path.read_text(encoding="utf-8")))
    return json.loads(str(row[1])), records


def _shares(state: AccountState) -> dict[str, int]:
    return {asset: state.shares(asset) for asset in ETF3 if state.shares(asset)}


def _ref_shares(engine: RefEngine) -> dict[str, int]:
    return {asset: engine.shares(asset) for asset in ETF3 if engine.shares(asset)}


def _load_item(cfg: V2Config, queue_item_id: str) -> tuple[ResearchProtocol, StrategyVersion, str]:
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


def _source_hash() -> str:
    path = Path(__file__).with_name("price_executor.py")
    strategy_path = Path(__file__).parents[1] / "strategies" / "price.py"
    return hashlib.sha256(path.read_bytes() + strategy_path.read_bytes()).hexdigest()


def _all_history(price_rows: list[dict[str, Any]]) -> list[PricePoint]:
    return [
        PricePoint(
            str(row["ts_code"]),
            _date(row["trade_date"]),
            datetime.combine(_date(row["trade_date"]), time(17, 0), SHANGHAI),
            _decimal(row, "close"),
            _decimal(row, "vol") if row.get("vol") is not None else None,
            SecurityType.ETF_DOMESTIC_EQUITY,
        )
        for row in price_rows
        if str(row.get("ts_code")) in ETF3
    ]


def _metric_payload(metrics) -> dict[str, Any]:
    return {
        item.metric_id: {
            "value": format(item.value, "f") if item.value is not None else None,
            "display_value": item.display_value,
            "unit": item.unit,
            "convention": item.convention,
            "version": item.version,
            "source_hash": item.source_hash,
        }
        for item in metrics
    }


def _write_strategy_evidence(cfg: V2Config, report: dict[str, Any]) -> str:
    """Write each strategy run to its own immutable evidence object."""

    run_id = str(report.get("research_run_id") or "unknown")
    target = cfg.paths.artifacts / "evidence" / "STRATEGY_RUNS" / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)
    return str(target.relative_to(cfg.paths.artifacts.parent))


def _monthly_returns(nav: list[tuple[str, Decimal]]) -> dict[str, str | None]:
    grouped: dict[str, list[Decimal]] = {}
    for on, value in nav:
        grouped.setdefault(on[:7], []).append(value)
    result: dict[str, str | None] = {}
    for key, values in grouped.items():
        result[key] = format(values[-1] / values[0] - Decimal(1), "f") if len(values) > 1 else None
    return result


def _validation_windows(protocol: ResearchProtocol, sessions: list[date]) -> list[dict[str, Any]]:
    declared = [("train", *protocol.train_window)] + [
        (f"validation_{index + 1}", *window)
        for index, window in enumerate(protocol.validation_windows)
    ]
    result: list[dict[str, Any]] = []
    for name, left_value, right_value in declared:
        left, right = _date(left_value), _date(right_value)
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


def _frozen_protocol_range(protocol: ResearchProtocol) -> tuple[date, date]:
    """Return the only range that may contribute to a frozen Stage09 decision."""

    return _date(protocol.train_window[0]), _date(protocol.validation_windows[-1][1])


def _replay(
    strategy_key: str,
    price_rows: list[dict[str, Any]],
    limit_rows: list[dict[str, Any]],
    start: date,
    end: date,
    initial_cash: Decimal,
) -> tuple[list[DailySnapshot], list[DailySnapshot], list[date], dict[str, Any]]:
    if strategy_key not in SUPPORTED_STRATEGIES:
        raise ValueError(f"strategy is not executable in ETF3 scope: {strategy_key}")
    prices = {
        (str(row["ts_code"]), _date(row["trade_date"])): row
        for row in price_rows
        if str(row.get("ts_code")) in ETF3
    }
    limits = {
        (str(row["ts_code"]), _date(row["trade_date"])): row
        for row in limit_rows
        if str(row.get("ts_code")) in ETF3
    }
    price_sessions = sorted({key[1] for key in prices})
    sessions = [
        session
        for session in price_sessions
        if all((asset, session) in prices and (asset, session) in limits for asset in ETF3)
    ]
    if not sessions:
        raise ValueError("no complete ETF3 sessions with both prices and execution limits")
    selected = [session for session in sessions if start <= session <= end]
    definition = STRATEGY_CATALOG[strategy_key]
    warm = [session for session in price_sessions if session < start]
    if len(warm) < definition.lookback + 1:
        raise ValueError(
            f"insufficient warmup for {strategy_key}: need {definition.lookback + 1}, got {len(warm)}"
        )
    if len(selected) < 3:
        raise ValueError("backtest range must contain at least three complete sessions")
    history = _all_history(price_rows)
    next_session = _next_session_factory(price_sessions)
    formal_state = AccountState(initial_cash)
    formal = FormalEngine(formal_state, default_rulebook(), next_session)
    reference = RefEngine(initial_cash, next_session)
    boards = {asset: Board.SSE_MAIN if asset.endswith(".SH") else Board.SZSE_MAIN for asset in ETF3}
    ref_boards = {asset: board.value for asset, board in boards.items()}
    types = {asset: SecurityType.ETF_DOMESTIC_EQUITY for asset in ETF3}
    ref_types = {asset: value.value for asset, value in types.items()}
    formal_days: list[DailySnapshot] = []
    reference_days: list[DailySnapshot] = []
    prior_month: tuple[int, int] | None = None
    rebalance_days: list[date] = []
    signal_records: list[dict[str, Any]] = []
    for on in selected:
        position = price_sessions.index(on)
        if position == 0:
            continue
        prior = price_sessions[position - 1]
        decision_time = datetime.combine(prior, time(17, 1), SHANGHAI)
        signal = run_strategy(strategy_key, history, decision_time, top_n=1)
        if any(day > prior for day in signal.evidence_sessions.values()):
            raise RuntimeError("strategy used a price point after its decision time")
        month = (on.year, on.month)
        should_rebalance = prior_month is None or month != prior_month
        prior_month = month
        formal_orders: list[Order] = []
        ref_orders: list[RefOrder] = []
        if should_rebalance:
            rebalance_days.append(on)
            prior_closes = {asset: _decimal(prices[(asset, prior)], "close") for asset in ETF3}
            nav = formal_state.nav(prior_closes)
            targets = {
                asset: int(
                    (
                        nav
                        * weight
                        * Decimal("0.98")
                        / prior_closes[asset]
                        / Decimal(100)
                    ).to_integral_value(rounding=ROUND_FLOOR)
                )
                * 100
                for asset, weight in signal.targets.items()
            }
            formal_orders = formal.plan_targets(_shares(formal_state), targets, on, boards, types)
            ref_orders = reference.plan_targets(_ref_shares(reference), targets, on, ref_boards, ref_types)
            for order in sorted(formal_orders, key=lambda item: item.side != "SELL"):
                row = prices[(order.asset_id, on)]
                limit = limits[(order.asset_id, on)]
                formal.execute(
                    order,
                    on,
                    _decimal(row, "open"),
                    market=MarketState(
                        True,
                        _decimal(limit, "up_limit"),
                        _decimal(limit, "down_limit"),
                        "execution-limits-v2",
                    ),
                )
            for ref_order in sorted(ref_orders, key=lambda item: item.side != "SELL"):
                row = prices[(ref_order.asset_id, on)]
                limit = limits[(ref_order.asset_id, on)]
                reference.execute(
                    ref_order,
                    on,
                    _decimal(row, "open"),
                    RefMarket(
                        True,
                        _decimal(limit, "up_limit"),
                        _decimal(limit, "down_limit"),
                        "execution-limits-v2",
                    ),
                )
            signal_records.append(
                {
                    "trade_date": on.isoformat(),
                    "decision_date": prior.isoformat(),
                    "strategy": strategy_key,
                    "scores": {key: format(value, "f") for key, value in signal.scores.items()},
                    "targets": {key: format(value, "f") for key, value in signal.targets.items()},
                }
            )
        marks = {asset: _decimal(prices[(asset, on)], "close") for asset in ETF3}
        formal_days.append(formal_snapshot(on, formal_state, formal_orders, formal.fills, marks))
        reference_days.append(reference_snapshot(on, reference, ref_orders, reference.fills, marks))
    if len(formal_days) != len(selected):
        raise RuntimeError("selected range contains an invalid first session")
    return formal_days, reference_days, rebalance_days, {"signals": signal_records, "sessions": selected}


def _persist_queue_run(
    cfg: V2Config,
    queue_item_id: str,
    protocol: ResearchProtocol,
    version: StrategyVersion,
    *,
    start: date | None = None,
    end: date | None = None,
    initial_cash: Decimal = INITIAL_CASH,
) -> dict[str, Any]:
    if protocol.family_id not in SUPPORTED_FAMILIES:
        raise ValueError(f"protocol family is not executable: {protocol.family_id}")
    strategy_key = str(protocol.protocol_id).replace("R01.ETF3.v2", "etf_rotation.v1").replace(
        "R05.ETF3_TREND.v1", "etf_trend_defensive.v1"
    ).replace("R02.ETF3_LOWVOL.v1", "etf_lowvol.v1").replace(
        "R06.ETF3_MOM20.v1", "etf_momentum_20.v1"
    ).replace("R07.ETF3_RISKADJ60.v1", "etf_risk_adjusted_60.v1")
    if strategy_key not in SUPPORTED_STRATEGIES:
        raise ValueError(f"protocol does not map to a registered executable strategy: {protocol.protocol_id}")
    assessment = assess_protocol(cfg.paths.state / "control.sqlite", {
        "family_id": protocol.family_id,
        "data_dependencies": list(protocol.data_dependencies),
        "train_window": list(protocol.train_window),
        "validation_windows": [list(x) for x in protocol.validation_windows],
    })
    if not assessment.ready:
        raise RuntimeError("dependency assessment failed: " + ",".join(assessment.reasons))
    price_manifest, price_rows = _publication(cfg, PRICE_DATASET)
    limit_manifest, limit_rows = _publication(cfg, LIMIT_DATASET)
    master_manifest, master_rows = _publication(cfg, MASTER_DATASET)
    master_assets = {str(row.get("ts_code")) for row in master_rows}
    if master_assets != set(ETF3):
        raise RuntimeError(f"ETF3 universe mismatch: {sorted(master_assets)}")
    complete = {
        _date(row["trade_date"])
        for row in limit_rows
        if str(row.get("ts_code")) in ETF3
    }
    for asset in ETF3:
        complete &= {
            _date(row["trade_date"])
            for row in limit_rows
            if str(row.get("ts_code")) == asset
        }
    protocol_start, protocol_end = _frozen_protocol_range(protocol)
    declared_start = _date(start or protocol_start)
    declared_end = _date(end or protocol_end)
    if (declared_start, declared_end) != (protocol_start, protocol_end):
        raise ValueError(
            "requested backtest range does not match the frozen research protocol: "
            f"requested={declared_start.isoformat()}..{declared_end.isoformat()}, "
            f"required={protocol_start.isoformat()}..{protocol_end.isoformat()}; "
            "the request is blocked instead of silently changing the Stage09 evaluation window"
        )
    complete = {x for x in complete if declared_start <= x <= declared_end}
    if not complete:
        raise RuntimeError("no complete limits in requested range")
    actual_start, actual_end = min(complete), max(complete)
    snapshot_base = {
        "strategy_version_id": version.strategy_version_id,
        "protocol_hash": protocol.protocol_hash,
        "price_manifest_hash": price_manifest["manifest_hash"],
        "limit_manifest_hash": limit_manifest["manifest_hash"],
        "master_manifest_hash": master_manifest["manifest_hash"],
        "requested_start": declared_start.isoformat(),
        "requested_end": declared_end.isoformat(),
        "actual_start": actual_start.isoformat(),
        "actual_end": actual_end.isoformat(),
        "initial_cash": format(initial_cash, "f"),
        "strategy_key": strategy_key,
    }
    snapshot_hash = canonical_hash(snapshot_base)
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=protocol.protocol_hash,
        snapshot_hash=snapshot_hash,
        start=actual_start,
        end=actual_end,
        purpose=AccessPurpose.RESEARCH,
        asset_scope="ETF3",
        family_scope=protocol.family_id,
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        raise RuntimeError(f"OOS authorization denied: {receipt.decision.value}")
    formal_days, reference_days, rebalance_days, replay_meta = _replay(
        strategy_key, price_rows, limit_rows, actual_start, actual_end, initial_cash
    )
    stage10 = compare_snapshots(tuple(formal_days), tuple(reference_days), input_hash=snapshot_hash)
    windows = _validation_windows(protocol, [date.fromisoformat(x.on) for x in formal_days])
    validation_sessions = {
        session.isoformat()
        for window, item in zip(protocol.validation_windows, windows[1:], strict=True)
        if item["session_count"] > 0
        for session in [date.fromisoformat(x.on) for x in formal_days]
        if _date(window[0]) <= session <= _date(window[1])
    }
    validation_days = [x for x in formal_days if x.on in validation_sessions]
    validation_nav = tuple((x.on, Decimal(x.nav)) for x in validation_days)
    full_nav = tuple((x.on, Decimal(x.nav)) for x in formal_days)
    benchmark_nav = tuple(
        (x.on, initial_cash * sum(
            (
                _decimal(next(row for row in price_rows if str(row.get("ts_code")) == asset and _date(row["trade_date"]) == _date(x.on)), "close")
                / _decimal(next(row for row in price_rows if str(row.get("ts_code")) == asset and _date(row["trade_date"]) == _date(formal_days[0].on)), "close")
                for asset in ETF3
            ),
            Decimal(0),
        ) / Decimal(len(ETF3)))
        for x in formal_days
    )
    calculator = MetricCalculator()
    canonical = _metric_payload(calculator.nav_metrics(full_nav))
    benchmark_metrics = _metric_payload(calculator.nav_metrics(benchmark_nav))
    strategy_total = Decimal(canonical["total_return"]["value"] or "0")
    benchmark_total = Decimal(benchmark_metrics["total_return"]["value"] or "0")
    primary_metric = strategy_total - benchmark_total
    effective_trades = len({fill.fill_id for snapshot in formal_days for fill in snapshot.fills})
    validation_blocks = sum(item["session_count"] > 0 for item in windows[1:])
    stage09 = evaluate_stage09(
        protocol,
        version,
        readiness=Readiness.REAL_READY,
        access_receipt=receipt,
        effective_trades=effective_trades,
        validation_blocks=validation_blocks,
        primary_metric=primary_metric,
        future_leakage=False,
        snapshot_hash=snapshot_hash,
    )
    run_id = f"real-{strategy_key.replace('.', '-')}-{snapshot_hash[:24]}-{hashlib.sha256(queue_item_id.encode()).hexdigest()[:12]}"
    existing_conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        existing_run = existing_conn.execute(
            "SELECT evidence_ref FROM research_runs WHERE research_run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        existing_conn.close()
    if existing_run is not None:
        existing_path = cfg.paths.artifacts.parent / str(existing_run[0])
        if existing_path.is_file():
            existing_report = json.loads(existing_path.read_text(encoding="utf-8"))
            existing_report["idempotent"] = True
            return existing_report
    stage09_id = f"stage09-{run_id}"
    evidence_report: dict[str, Any] = {
        "status": "SUCCEEDED",
        "execution_scope": "STRATEGY",
        "strategy_key": strategy_key,
        "queue_item_id": queue_item_id,
        "research_run_id": run_id,
        "protocol_id": protocol.protocol_id,
        "protocol_hash": protocol.protocol_hash,
        "strategy_version_id": version.strategy_version_id,
        "snapshot_hash": snapshot_hash,
        "implementation_hash": _source_hash(),
        "requested_range": {"start": declared_start.isoformat(), "end": declared_end.isoformat()},
        "actual_range": {"start": actual_start.isoformat(), "end": actual_end.isoformat()},
        "selected_parameters": {"strategy": strategy_key, "top_n": 1, "rebalance": "MONTHLY", "capital_fraction": "0.98"},
        "dataset": {
            "prices": {"dataset": PRICE_DATASET, "manifest_id": price_manifest["manifest_id"], "manifest_hash": price_manifest["manifest_hash"], "row_count": price_manifest.get("row_count"), "assets": list(ETF3)},
            "limits": {"dataset": LIMIT_DATASET, "manifest_id": limit_manifest["manifest_id"], "manifest_hash": limit_manifest["manifest_hash"], "row_count": limit_manifest.get("row_count"), "complete_asset_days": len(complete)},
            "master": {"dataset": MASTER_DATASET, "manifest_id": master_manifest["manifest_id"], "manifest_hash": master_manifest["manifest_hash"], "row_count": master_manifest.get("row_count")},
        },
        "windows": windows,
        "rebalance_days": [x.isoformat() for x in rebalance_days],
        "oos_receipt_id": receipt.receipt_id,
        "oos_decision": receipt.decision.value,
        "metrics": {
            "canonical": canonical,
            "benchmark": benchmark_metrics,
            "primary_metric": format(primary_metric, "f"),
            "primary_metric_name": protocol.primary_metric,
            "effective_trades": effective_trades,
            "validation_blocks": validation_blocks,
            "monthly_returns": _monthly_returns(list(full_nav)),
            "validation_nav_points": len(validation_nav),
            "turnover": {
                "rebalance_count": len(rebalance_days),
                "filled_orders": effective_trades,
            },
        },
        "robustness": {
            "parameter_stability": "REGISTERED_GRID_NOT_EXPANDED",
            "cost_stress": "FROZEN_RULEBOOK_ONLY;2X_NOT_RUN",
            "subperiods": 3,
            "regime": "NOT_CLASSIFIED_WITHIN_BOUNDED_ETF3_PROTOCOL",
            "future_rows_filtered_by_available_at": True,
        },
        "stage09": {
            "result_id": stage09_id,
            "rating": stage09.rating.value,
            "reasons": list(stage09.reasons),
            "primary_metric": format(stage09.primary_metric, "f") if stage09.primary_metric is not None else None,
            "effective_trades": stage09.effective_trades,
            "validation_blocks": stage09.validation_blocks,
        },
        "stage10": {
            "status": stage10.status.value,
            "evidence_valid": stage10.evidence_valid,
            "formal_hash": stage10.formal_hash,
            "reference_hash": stage10.reference_hash,
            "differences": len(stage10.differences),
        },
        "daily_snapshots": [
            {
                "trade_date": item.on,
                "nav": item.nav,
                "cash": item.cash,
                "total_fees": item.total_fees,
                "positions": [{"asset_id": asset, "shares": shares} for asset, shares in item.positions],
                "orders": [item.__dict__ for item in item.orders],
                "fills": [item.__dict__ for item in item.fills],
            }
            for item in formal_days
        ],
        "signals": replay_meta["signals"],
        "synthetic": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    evidence_ref = _write_strategy_evidence(cfg, evidence_report)
    metrics = evidence_report["metrics"]
    _insert_research_run(
        cfg,
        research_run_id=run_id,
        queue_item_id=queue_item_id,
        version=version,
        protocol=protocol,
        snapshot_hash=snapshot_hash,
        receipt_id=receipt.receipt_id,
        implementation_hash=evidence_report["implementation_hash"],
        dataset_manifest=evidence_report["dataset"],
        windows=windows,
        metrics=metrics,
        robustness=evidence_report["robustness"],
        evidence_ref=evidence_ref,
        execution_scope="STRATEGY",
        execution_rules="ETF3_OHLC_OPEN_NEXT_SESSION_FORMAL_V1",
    )
    store = ResearchStore(cfg.paths.state / "control.sqlite")
    current = _load_item(cfg, queue_item_id)[2]
    if current in {QueueStatus.PLANNED.value, QueueStatus.BLOCKED_DATA.value, QueueStatus.READY.value}:
        if current != QueueStatus.READY.value:
            store.transition(queue_item_id, QueueStatus.READY)
        store.transition(queue_item_id, QueueStatus.RUNNING)
    if _load_item(cfg, queue_item_id)[2] == QueueStatus.RUNNING.value:
        store.transition(queue_item_id, QueueStatus.SUCCEEDED)
    store.record_stage09(stage09_id, queue_item_id, stage09)
    _record_stage09_evidence(
        cfg,
        result_id=stage09_id,
        research_run_id=run_id,
        version=version,
        protocol=protocol,
        result=stage09,
        receipt_id=receipt.receipt_id,
        dataset_manifest=evidence_report["dataset"],
        windows=windows,
        metrics=metrics,
        robustness=evidence_report["robustness"],
        execution_rules="ETF3_OHLC_OPEN_NEXT_SESSION_FORMAL_V1",
    )
    stage10_id = Stage10Store(cfg.paths.state / "control.sqlite").record(run_id, stage10)
    reporting_publication_id = f"research-{snapshot_hash[:20]}-{run_id[-12:]}"
    ReportingStore(cfg.paths.state / "control.sqlite").publish(
        reporting_publication_id,
        actual_end,
        "REAL",
        "PASS" if stage10.status.value == "PASS" else "FAIL",
        snapshot_hash,
        run_id,
        {("STRATEGY", strategy_key): calculator.nav_metrics(full_nav)},
        f"stage10:{stage10_id}",
    )
    evidence_report["stage10"]["result_id"] = stage10_id
    evidence_report["reporting_publication_id"] = reporting_publication_id
    evidence_report["evidence_ref"] = evidence_ref
    _write_strategy_evidence(cfg, evidence_report)
    return evidence_report


def run_registered_queue_item(cfg: V2Config, queue_item_id: str) -> dict[str, Any]:
    protocol, version, status = _load_item(cfg, queue_item_id)
    if status == QueueStatus.SUCCEEDED.value:
        conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
        try:
            row = conn.execute("SELECT research_run_id,evidence_ref FROM research_runs WHERE queue_item_id=? ORDER BY created_at DESC LIMIT 1", (queue_item_id,)).fetchone()
        finally:
            conn.close()
        if row:
            path = cfg.paths.artifacts.parent / str(row[1])
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if str(payload.get("research_run_id")) == str(row[0]):
                    payload["idempotent"] = True
                    return payload
    return _persist_queue_run(cfg, queue_item_id, protocol, version)


def run_direct_backtest(
    cfg: V2Config,
    strategy_id: str,
    start_date: str,
    end_date: str,
    initial_cash: Decimal = INITIAL_CASH,
) -> dict[str, Any]:
    """Run the registered frozen protocol range without opening a new OOS window."""

    if strategy_id not in SUPPORTED_STRATEGIES:
        raise ValueError(f"unsupported strategy_id: {strategy_id}")
    protocol_id = {
        "etf_rotation.v1": "R01.ETF3.v2",
        "etf_lowvol.v1": "R02.ETF3_LOWVOL.v1",
        "etf_trend_defensive.v1": "R05.ETF3_TREND.v1",
        "etf_momentum_20.v1": "R06.ETF3_MOM20.v1",
        "etf_risk_adjusted_60.v1": "R07.ETF3_RISKADJ60.v1",
    }[strategy_id]
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        row = conn.execute(
            "SELECT q.queue_item_id FROM research_queue q JOIN research_protocols p ON p.protocol_hash=q.protocol_hash WHERE p.protocol_id=? ORDER BY q.created_at DESC LIMIT 1",
            (protocol_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"registered executable protocol is missing: {protocol_id}")
    protocol, version, _ = _load_item(cfg, str(row[0]))
    return _persist_queue_run(
        cfg,
        str(row[0]),
        protocol,
        version,
        start=_date(start_date),
        end=_date(end_date),
        initial_cash=initial_cash,
    )


__all__ = ["SUPPORTED_STRATEGIES", "run_direct_backtest", "run_registered_queue_item"]
