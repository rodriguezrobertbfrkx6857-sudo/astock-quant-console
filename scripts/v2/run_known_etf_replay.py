from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reference_engine.engine import RefEngine, RefMarket  # noqa: E402
from src.quantcore.accounting.ledger import AccountState  # noqa: E402
from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.data.manifest import ManifestStore  # noqa: E402
from src.quantcore.execution.formal import FormalEngine, MarketState  # noqa: E402
from src.quantcore.research.oos import (  # noqa: E402
    AccessDecision,
    AccessPurpose,
    OOSAccessService,
)
from src.quantcore.reporting.metrics import (  # noqa: E402
    MetricCalculator,
    ReportingStore,
)
from src.quantcore.rules.rulebook import default_rulebook  # noqa: E402
from src.quantcore.strategies import PricePoint, run_strategy  # noqa: E402
from src.quantcore.universe.models import Board, SecurityType  # noqa: E402
from src.quantcore.validation.projection import (  # noqa: E402
    formal_snapshot,
    reference_snapshot,
)
from src.quantcore.validation.stage10 import (  # noqa: E402
    Stage10Status,
    Stage10Store,
    compare_snapshots,
)


ETF3 = ("510300.SH", "510500.SH", "159915.SZ")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _load_publication(cfg, dataset: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
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
        raise ValueError(f"missing REAL_READY publication: {dataset}")
    store = ManifestStore(cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256")
    manifest = store.load(str(row[0]))
    records: list[dict[str, object]] = []
    for digest in manifest.object_hashes:
        path = store.object_root / digest[:2] / digest
        records.extend(json.loads(path.read_text("utf-8")))
    return json.loads(str(row[1])), records


def _decimal(row: dict[str, object], field: str) -> Decimal:
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


def _shares(state: AccountState) -> dict[str, int]:
    return {asset: state.shares(asset) for asset in ETF3 if state.shares(asset)}


def _ref_shares(engine: RefEngine) -> dict[str, int]:
    return {asset: engine.shares(asset) for asset in ETF3 if engine.shares(asset)}


def run() -> dict[str, object]:
    cfg = load_v2_config(create_dirs=True)
    master_manifest, master_rows = _load_publication(cfg, "historical_etf_master")
    price_manifest, price_rows = _load_publication(cfg, "known_etf3_prices")
    limit_manifest, limit_rows = _load_publication(cfg, "known_etf3_limits")
    if {str(row.get("ts_code")) for row in master_rows} != set(ETF3):
        raise ValueError("historical ETF master does not exactly match ETF3")

    prices = {
        (str(row["ts_code"]), date.fromisoformat(str(row["trade_date"]))): row
        for row in price_rows
    }
    limits = {
        (str(row["ts_code"]), date.fromisoformat(str(row["trade_date"]))): row
        for row in limit_rows
    }
    sessions = sorted(
        session
        for session in {key[1] for key in prices}
        if all((asset, session) in prices for asset in ETF3)
    )
    if len(sessions) < 80:
        raise ValueError("replay requires at least 60 warmup + 20 complete sessions")
    replay_sessions: list[date] | None = None
    for end_index in range(len(sessions) - 1, 78, -1):
        candidate = sessions[end_index - 19 : end_index + 1]
        if all((asset, session) in limits for session in candidate for asset in ETF3):
            replay_sessions = candidate
            break
    if replay_sessions is None:
        raise ValueError("no consecutive 20-session window has complete execution limits")
    snapshot_hash = hashlib.sha256(
        "|".join(
            (
                str(master_manifest["manifest_hash"]),
                str(price_manifest["manifest_hash"]),
                str(limit_manifest["manifest_hash"]),
            )
        ).encode()
    ).hexdigest()
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        protocol_hash = str(
            conn.execute(
                "SELECT protocol_hash FROM research_protocols WHERE protocol_id='R01.v1'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=protocol_hash,
        snapshot_hash=snapshot_hash,
        start=replay_sessions[0],
        end=replay_sessions[-1],
        purpose=AccessPurpose.RESEARCH,
        asset_scope="ETF3",
        family_scope="ETF_ROTATION",
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        raise ValueError(f"OOS authorization denied: {receipt.decision.value}")

    next_session = _next_session_factory(sessions)
    formal_state = AccountState(Decimal("1000000"))
    formal = FormalEngine(formal_state, default_rulebook(), next_session)
    reference = RefEngine(Decimal("1000000"), next_session)
    formal_days = []
    reference_days = []
    boards = {
        asset: Board.SSE_MAIN if asset.endswith(".SH") else Board.SZSE_MAIN
        for asset in ETF3
    }
    ref_boards = {asset: board.value for asset, board in boards.items()}
    types = {asset: SecurityType.ETF_DOMESTIC_EQUITY for asset in ETF3}
    ref_types = {asset: value.value for asset, value in types.items()}
    all_history = [
        PricePoint(
            asset,
            session,
            datetime.combine(session, time(17, 0), SHANGHAI),
            _decimal(row, "close"),
            _decimal(row, "vol") if row.get("vol") is not None else None,
            SecurityType.ETF_DOMESTIC_EQUITY,
        )
        for (asset, session), row in prices.items()
    ]
    prior_month: tuple[int, int] | None = None
    rebalance_days = []
    for on in replay_sessions:
        position = sessions.index(on)
        prior = sessions[position - 1]
        decision_time = datetime.combine(prior, time(17, 1), SHANGHAI)
        signal = run_strategy(
            "etf_rotation.v1", all_history, decision_time, top_n=1
        )
        if not signal.targets or any(day > prior for day in signal.evidence_sessions.values()):
            raise ValueError("strategy produced no causal target")
        month = (on.year, on.month)
        should_rebalance = prior_month is None or month != prior_month
        prior_month = month
        formal_orders = []
        ref_orders = []
        if should_rebalance:
            rebalance_days.append(on.isoformat())
            prior_closes = {
                asset: _decimal(prices[(asset, prior)], "close") for asset in ETF3
            }
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
            formal_orders = formal.plan_targets(
                _shares(formal_state), targets, on, boards, types
            )
            ref_orders = reference.plan_targets(
                _ref_shares(reference), targets, on, ref_boards, ref_types
            )
            for formal_order in sorted(
                formal_orders, key=lambda item: item.side != "SELL"
            ):
                row = prices[(formal_order.asset_id, on)]
                limit = limits[(formal_order.asset_id, on)]
                formal.execute(
                    formal_order,
                    on,
                    _decimal(row, "open"),
                    market=MarketState(
                        True,
                        _decimal(limit, "up_limit"),
                        _decimal(limit, "down_limit"),
                        str(limit_manifest["manifest_hash"]),
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
                        str(limit_manifest["manifest_hash"]),
                    ),
                )
        marks = {asset: _decimal(prices[(asset, on)], "close") for asset in ETF3}
        formal_days.append(
            formal_snapshot(on, formal_state, formal_orders, formal.fills, marks)
        )
        reference_days.append(
            reference_snapshot(on, reference, ref_orders, reference.fills, marks)
        )

    stage10 = compare_snapshots(
        tuple(formal_days), tuple(reference_days), input_hash=snapshot_hash
    )
    run_id = f"real-etf3-replay-{replay_sessions[0]}-{replay_sessions[-1]}"
    stage10_id = Stage10Store(cfg.paths.state / "control.sqlite").record(
        run_id, stage10
    )
    status = (
        "PASS"
        if len(formal_days) == 20 and stage10.status is Stage10Status.PASS
        else "FAIL"
    )
    publication_id = f"real-etf3-replay-{snapshot_hash[:16]}"
    nav_series = tuple(
        (snapshot.on, Decimal(snapshot.nav)) for snapshot in formal_days
    )
    daily_snapshots = [
        {
            "trade_date": snapshot.on,
            "nav": snapshot.nav,
            "cash": snapshot.cash,
            "total_fees": snapshot.total_fees,
            "positions": [
                {"ts_code": asset_id, "shares": shares}
                for asset_id, shares in snapshot.positions
            ],
            "orders": [
                {
                    "order_id": order.order_id,
                    "asset_id": order.asset_id,
                    "side": order.side,
                    "requested_shares": order.requested_shares,
                    "filled_shares": order.filled_shares,
                    "status": order.status,
                }
                for order in snapshot.orders
            ],
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "asset_id": fill.asset_id,
                    "side": fill.side,
                    "shares": fill.shares,
                    "price": fill.price,
                    "fee": fill.fee,
                    "rule_id": fill.rule_id,
                }
                for fill in snapshot.fills
            ],
        }
        for snapshot in formal_days
    ]
    orders_detail = [
        order
        for snapshot in daily_snapshots
        for order in snapshot["orders"]
    ]
    fills_detail = [
        fill
        for snapshot in daily_snapshots
        for fill in snapshot["fills"]
    ]
    ReportingStore(cfg.paths.state / "control.sqlite").publish(
        publication_id,
        replay_sessions[-1],
        "REAL",
        "PASS" if status == "PASS" else "FAIL",
        snapshot_hash,
        run_id,
        {("PORTFOLIO", "ETF3_REPLAY"): MetricCalculator().nav_metrics(nav_series)},
        f"stage10:{stage10_id}",
    )
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
            "detail=excluded.detail,evidence_ref=excluded.evidence_ref,"
            "updated_at=excluded.updated_at",
            (
                "historical_20d_replay",
                status,
                f"sessions={len(formal_days)}; stage10={stage10.status.value}",
                f"stage10:{stage10_id}",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    report = {
        "status": status,
        "scope": "ETF3_KNOWN",
        "strategy": "etf_rotation.v1",
        "sessions": [item.isoformat() for item in replay_sessions],
        "session_count": len(formal_days),
        "warmup_sessions": sessions.index(replay_sessions[0]),
        "consecutive_price_calendar_sessions": True,
        "rebalance_days": rebalance_days,
        "oos_receipt_id": receipt.receipt_id,
        "oos_decision": receipt.decision.value,
        "input_manifest_hashes": {
            "historical_etf_master": master_manifest["manifest_hash"],
            "prices": price_manifest["manifest_hash"],
            "limits": limit_manifest["manifest_hash"],
        },
        "stage10_result_id": stage10_id,
        "stage10_status": stage10.status.value,
        "stage10_differences": len(stage10.differences),
        "formal_hash": stage10.formal_hash,
        "reference_hash": stage10.reference_hash,
        "final_nav": formal_days[-1].nav,
        "total_return": float(Decimal(formal_days[-1].nav) / Decimal("1000000") - 1),
        "nav_series": [
            {"trade_date": on, "nav": format(nav, "f")} for on, nav in nav_series
        ],
        "daily_snapshots": daily_snapshots,
        "orders_detail": orders_detail,
        "fills_detail": fills_detail,
        "reporting_publication_id": publication_id,
        "fills": len(formal.fills),
        "synthetic": False,
        "future_rows_filtered_by_available_at": True,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    target = (
        cfg.paths.artifacts
        / "evidence"
        / "REAL_OPERATION"
        / "historical_20d_replay.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    os.replace(temporary, target)
    return report


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
