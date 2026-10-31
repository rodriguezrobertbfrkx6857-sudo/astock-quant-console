from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    asset_id: str
    side: str
    requested_shares: int
    filled_shares: int
    status: str


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    order_id: str
    asset_id: str
    side: str
    shares: int
    price: str
    fee: str
    rule_id: str


@dataclass(frozen=True)
class DailySnapshot:
    on: str
    orders: tuple[OrderRecord, ...]
    fills: tuple[FillRecord, ...]
    cash: str
    positions: tuple[tuple[str, int], ...]
    total_fees: str
    nav: str
    event_receipts: tuple[str, ...] = ()
