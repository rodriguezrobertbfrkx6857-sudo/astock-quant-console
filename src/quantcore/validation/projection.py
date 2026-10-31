from __future__ import annotations

from datetime import date
from decimal import Decimal

from reference_engine.engine import RefEngine, RefFill, RefOrder
from src.quantcore.accounting.ledger import AccountState
from src.quantcore.contracts.execution import DailySnapshot, FillRecord, OrderRecord
from src.quantcore.execution.formal import Fill, Order


def _positions(lots) -> tuple[tuple[str, int], ...]:
    totals: dict[str, int] = {}
    for lot in lots:
        totals[lot.asset_id] = totals.get(lot.asset_id, 0) + lot.shares
    return tuple(
        sorted((asset_id, shares) for asset_id, shares in totals.items() if shares)
    )


def _order_record(order) -> OrderRecord:
    return OrderRecord(
        order.order_id,
        order.asset_id,
        order.side,
        order.shares,
        order.filled_shares,
        str(order.status),
    )


def _fill_record(fill) -> FillRecord:
    return FillRecord(
        fill.fill_id,
        fill.order_id,
        fill.asset_id,
        fill.side,
        fill.shares,
        format(fill.price, "f"),
        format(fill.fee, "f"),
        fill.rule_id,
    )


def formal_snapshot(
    on: date,
    state: AccountState,
    orders: list[Order],
    fills: list[Fill],
    prices: dict[str, Decimal],
    *,
    event_receipts: tuple[str, ...] = (),
) -> DailySnapshot:
    daily = [fill for fill in fills if fill.on == on]
    return DailySnapshot(
        on.isoformat(),
        tuple(sorted((_order_record(x) for x in orders), key=lambda x: x.order_id)),
        tuple(sorted((_fill_record(x) for x in daily), key=lambda x: x.fill_id)),
        format(state.cash, "f"),
        _positions(state.lots),
        format(sum((x.fee for x in daily), Decimal()), "f"),
        format(state.nav(prices), "f"),
        tuple(sorted(event_receipts)),
    )


def reference_snapshot(
    on: date,
    engine: RefEngine,
    orders: list[RefOrder],
    fills: list[RefFill],
    prices: dict[str, Decimal],
    *,
    event_receipts: tuple[str, ...] = (),
) -> DailySnapshot:
    daily = [fill for fill in fills if fill.on == on]
    return DailySnapshot(
        on.isoformat(),
        tuple(sorted((_order_record(x) for x in orders), key=lambda x: x.order_id)),
        tuple(sorted((_fill_record(x) for x in daily), key=lambda x: x.fill_id)),
        format(engine.cash, "f"),
        _positions(engine.lots),
        format(sum((x.fee for x in daily), Decimal()), "f"),
        format(engine.nav(prices), "f"),
        tuple(sorted(event_receipts)),
    )
