from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class RefLot:
    lot_id: str
    asset_id: str
    shares: int
    unit_cost: Decimal
    acquired_on: date
    available_on: date


@dataclass
class RefOrder:
    order_id: str
    asset_id: str
    side: str
    shares: int
    created_on: date
    board: str
    security_type: str = "STOCK"
    filled_shares: int = 0
    status: str = "PENDING"
    blocked_reason: str | None = None


@dataclass(frozen=True)
class RefFill:
    fill_id: str
    order_id: str
    asset_id: str
    side: str
    on: date
    shares: int
    price: Decimal
    gross: Decimal
    fee: Decimal
    rule_id: str


@dataclass(frozen=True)
class RefMarket:
    tradable: bool | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    source_ref: str


@dataclass(frozen=True)
class RefAction:
    action_id: str
    asset_id: str
    kind: str
    ex_date: date
    pay_date: date | None
    cash_per_share: Decimal = Decimal("0")
    stock_ratio: Decimal = Decimal("0")


class RefEngine:
    """Calculation-isolated engine; this module imports no Formal/accounting/rule code."""

    def __init__(self, cash: Decimal, next_session, lots: list[RefLot] | None = None):
        self.cash = cash
        self.next_session = next_session
        self.lots = list(lots or [])
        self.receivables: dict[str, Decimal] = {}
        self.fills: list[RefFill] = []
        self.receipts: set[str] = set()
        self._fill_receipts: dict[tuple[str, date], RefFill] = {}

    @staticmethod
    def plan_targets(
        current: dict[str, int],
        targets: dict[str, int],
        on: date,
        boards: dict[str, str],
        types: dict[str, str] | None = None,
    ) -> list[RefOrder]:
        orders: list[RefOrder] = []
        for asset_id in sorted(set(current) | set(targets)):
            delta = targets.get(asset_id, 0) - current.get(asset_id, 0)
            if delta:
                side = "BUY" if delta > 0 else "SELL"
                shares = abs(delta)
                order_id = hashlib.sha256(f"{on}|{asset_id}|{side}|{shares}".encode()).hexdigest()
                orders.append(
                    RefOrder(
                        order_id,
                        asset_id,
                        side,
                        shares,
                        on,
                        boards[asset_id],
                        (types or {}).get(asset_id, "STOCK"),
                    )
                )
        return orders

    @staticmethod
    def _rule(
        on: date, board: str, security_type: str
    ) -> tuple[str, int, int, Decimal, int]:
        if on < date(2018, 1, 1):
            raise RuntimeError("unsupported rule date")
        suffix = "pre-20230828" if on < date(2023, 8, 28) else "post-20230828"
        tax = Decimal("0.001") if on < date(2023, 8, 28) else Decimal("0.0005")
        if security_type == "STOCK" and board in {"SSE_MAIN", "SZSE_MAIN", "CHINEXT"}:
            return f"ashare-main-{suffix}", 100, 100, tax, 1
        if security_type == "STOCK" and board == "STAR" and on >= date(2019, 7, 22):
            return f"star-{suffix}", 200, 1, tax, 1
        if board in {"SSE_MAIN", "SZSE_MAIN"} and security_type == "ETF_DOMESTIC_EQUITY":
            return "domestic-equity-etf-v1", 100, 100, Decimal("0"), 1
        if board in {"SSE_MAIN", "SZSE_MAIN"} and security_type in {
            "ETF_BOND",
            "ETF_GOLD_COMMODITY",
            "ETF_CROSS_BORDER",
            "ETF_MONEY",
        }:
            return "t0-etf-v1", 100, 100, Decimal("0"), 0
        raise RuntimeError("unsupported board/date")

    @staticmethod
    def _fee(side: str, gross: Decimal, stamp_rate: Decimal) -> Decimal:
        commission = max((gross * Decimal("0.00025")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("5.00"))
        stamp = (gross * stamp_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if side == "SELL" else Decimal("0")
        return commission + stamp

    def shares(self, asset_id: str) -> int:
        return sum(lot.shares for lot in self.lots if lot.asset_id == asset_id)

    def execute(self, order: RefOrder, on: date, price: Decimal, market: RefMarket, max_fill: int | None = None) -> RefFill | None:
        key = (order.order_id, on)
        if key in self._fill_receipts:
            return self._fill_receipts[key]
        if price <= 0:
            raise ValueError("execution price must be positive")
        if market.tradable is not True or market.limit_up is None or market.limit_down is None or not market.source_ref:
            order.status, order.blocked_reason = "BLOCKED", "MARKET_STATE_UNKNOWN"
            return None
        if (order.side == "BUY" and price >= market.limit_up) or (order.side == "SELL" and price <= market.limit_down):
            order.status, order.blocked_reason = "BLOCKED", "LIMIT_LOCKED"
            return None
        rule_id, minimum, increment, stamp_rate, settlement_sessions = self._rule(
            on, order.board, order.security_type
        )
        quantity = min(order.shares - order.filled_shares, max_fill if max_fill is not None else order.shares)
        if order.side == "BUY":
            if quantity < minimum:
                order.status, order.blocked_reason = "BLOCKED", "QUANTITY_RULE"
                return None
            quantity = minimum + ((quantity - minimum) // increment) * increment
        else:
            available = sum(lot.shares for lot in self.lots if lot.asset_id == order.asset_id and lot.available_on <= on)
            quantity = min(quantity, available)
            if quantity <= 0:
                order.status, order.blocked_reason = "BLOCKED", "T1_OR_INSUFFICIENT_SHARES"
                return None
        gross = (Decimal(quantity) * price).quantize(Decimal("0.01"))
        fee = self._fee(order.side, gross, stamp_rate)
        fill_id = hashlib.sha256(f"{order.order_id}|{on}|{order.filled_shares}|{quantity}".encode()).hexdigest()
        if order.side == "BUY":
            if self.cash < gross + fee:
                order.status, order.blocked_reason = "BLOCKED", "INSUFFICIENT_CASH"
                return None
            self.cash -= gross + fee
            available_on = on
            for _ in range(settlement_sessions):
                available_on = self.next_session(available_on)
            self.lots.append(
                RefLot(fill_id, order.asset_id, quantity, price, on, available_on)
            )
        else:
            remaining = quantity
            kept: list[RefLot] = []
            for lot in self.lots:
                if lot.asset_id != order.asset_id or lot.available_on > on or remaining == 0:
                    kept.append(lot)
                    continue
                used = min(lot.shares, remaining)
                lot.shares -= used
                remaining -= used
                if lot.shares:
                    kept.append(lot)
            if remaining:
                raise RuntimeError("reference lot underflow")
            self.lots = kept
            self.cash += gross - fee
        fill = RefFill(fill_id, order.order_id, order.asset_id, order.side, on, quantity, price, gross, fee, rule_id)
        self.fills.append(fill)
        self._fill_receipts[key] = fill
        order.filled_shares += quantity
        order.status = "FILLED" if order.filled_shares == order.shares else "PARTIAL"
        return fill

    def apply_action(self, action: RefAction, on: date) -> None:
        if action.kind == "CASH_DIVIDEND":
            if on == action.ex_date and f"{action.action_id}:accrual" not in self.receipts:
                self.receivables[action.action_id] = (Decimal(self.shares(action.asset_id)) * action.cash_per_share).quantize(Decimal("0.01"))
                self.receipts.add(f"{action.action_id}:accrual")
            if action.pay_date == on and f"{action.action_id}:payment" not in self.receipts:
                self.cash += self.receivables.pop(action.action_id, Decimal())
                self.receipts.add(f"{action.action_id}:payment")
        elif action.kind == "STOCK_DIVIDEND" and on == action.ex_date:
            receipt = f"{action.action_id}:stock"
            if receipt not in self.receipts:
                for lot in self.lots:
                    if lot.asset_id == action.asset_id:
                        lot.shares += int(Decimal(lot.shares) * action.stock_ratio)
                self.receipts.add(receipt)
        elif action.kind not in {"CASH_DIVIDEND", "STOCK_DIVIDEND"}:
            raise ValueError("unsupported corporate action")

    def nav(self, prices: dict[str, Decimal]) -> Decimal:
        return self.cash + sum(self.receivables.values(), Decimal()) + sum(Decimal(lot.shares) * prices[lot.asset_id] for lot in self.lots)
