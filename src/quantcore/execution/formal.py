from __future__ import annotations
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from src.quantcore.accounting.ledger import AccountState, JournalLine, Lot
from src.quantcore.rules.rulebook import RuleBook
from src.quantcore.universe.models import Board, SecurityType


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    BLOCKED = "BLOCKED"


@dataclass
class Order:
    order_id: str
    asset_id: str
    side: str
    shares: int
    created_on: date
    board: Board
    security_type: SecurityType
    filled_shares: int = 0
    status: OrderStatus = OrderStatus.PENDING
    blocked_reason: str | None = None

    def __post_init__(self):
        if self.side not in {"BUY", "SELL"} or self.shares <= 0:
            raise ValueError("order must have BUY/SELL and positive shares")


@dataclass(frozen=True)
class Fill:
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
class CorporateAction:
    action_id: str
    asset_id: str
    kind: str
    ex_date: date
    pay_date: date | None
    cash_per_share: Decimal = Decimal("0")
    stock_ratio: Decimal = Decimal("0")


@dataclass(frozen=True)
class MarketState:
    tradable: bool | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    source_ref: str


class FormalEngine:
    def __init__(self, state: AccountState, rulebook: RuleBook, next_session):
        self.state = state
        self.rulebook = rulebook
        self.next_session = next_session
        self.fills: list[Fill] = []
        self._fill_receipts: dict[tuple[str, date], Fill] = {}

    @staticmethod
    def plan_targets(
        current: dict[str, int],
        targets: dict[str, int],
        on: date,
        boards: dict[str, Board],
        types: dict[str, SecurityType],
    ) -> list[Order]:
        orders = []
        for asset in sorted(set(current) | set(targets)):
            delta = targets.get(asset, 0) - current.get(asset, 0)
            if delta:
                side = "BUY" if delta > 0 else "SELL"
                shares = abs(delta)
                oid = hashlib.sha256(
                    f"{on}|{asset}|{side}|{shares}".encode()
                ).hexdigest()
                orders.append(
                    Order(oid, asset, side, shares, on, boards[asset], types[asset])
                )
        return orders

    def execute(
        self,
        order: Order,
        on: date,
        price: Decimal,
        *,
        market: MarketState,
        max_fill: int | None = None,
    ) -> Fill | None:
        key = (order.order_id, on)
        if key in self._fill_receipts:
            return self._fill_receipts[key]
        if price <= 0:
            raise ValueError("execution price must be positive")
        if (
            market.tradable is not True
            or market.limit_up is None
            or market.limit_down is None
            or not market.source_ref
        ):
            order.status = OrderStatus.BLOCKED
            order.blocked_reason = "MARKET_STATE_UNKNOWN"
            return None
        if (order.side == "BUY" and price >= market.limit_up) or (
            order.side == "SELL" and price <= market.limit_down
        ):
            order.status = OrderStatus.BLOCKED
            order.blocked_reason = "LIMIT_LOCKED"
            return None
        rule = self.rulebook.resolve(on, order.board, order.security_type)
        remaining = order.shares - order.filled_shares
        quantity = min(remaining, max_fill if max_fill is not None else remaining)
        if order.side == "BUY":
            if quantity < rule.buy_minimum:
                order.status = OrderStatus.BLOCKED
                order.blocked_reason = "QUANTITY_RULE"
                return None
            quantity = (
                rule.buy_minimum
                + ((quantity - rule.buy_minimum) // rule.buy_increment)
                * rule.buy_increment
            )
            rule.validate_buy(quantity)
        else:
            quantity = min(quantity, self.state.available_shares(order.asset_id, on))
            if quantity <= 0:
                order.status = OrderStatus.BLOCKED
                order.blocked_reason = "T1_OR_INSUFFICIENT_SHARES"
                return None
        gross = (Decimal(quantity) * price).quantize(Decimal("0.01"))
        fee = rule.fees(order.side, gross)
        fid = hashlib.sha256(
            f"{order.order_id}|{on}|{order.filled_shares}|{quantity}".encode()
        ).hexdigest()
        if order.side == "BUY":
            total = gross + fee
            if self.state.cash < total:
                order.status = OrderStatus.BLOCKED
                order.blocked_reason = "INSUFFICIENT_CASH"
                return None
            self.state.cash -= total
            available_on = on
            for _ in range(rule.settlement_sessions):
                available_on = self.next_session(available_on)
            self.state.lots.append(
                Lot(fid, order.asset_id, quantity, price, on, available_on)
            )
            self.state.post(
                fid,
                on,
                (
                    JournalLine("POSITION_COST", debit=gross),
                    JournalLine("FEE_EXPENSE", debit=fee),
                    JournalLine("CASH", credit=total),
                ),
            )
        else:
            cost = self._consume_fifo(order.asset_id, quantity, on)
            net = gross - fee
            self.state.cash += net
            self.state.post(
                fid,
                on,
                (
                    JournalLine("CASH", debit=net),
                    JournalLine("FEE_EXPENSE", debit=fee),
                    JournalLine("POSITION_COST", credit=cost),
                    JournalLine("REALIZED_PNL", credit=gross - cost),
                ),
            )
        fill = Fill(
            fid,
            order.order_id,
            order.asset_id,
            order.side,
            on,
            quantity,
            price,
            gross,
            fee,
            rule.rule_id,
        )
        self.fills.append(fill)
        self._fill_receipts[key] = fill
        order.filled_shares += quantity
        order.status = (
            OrderStatus.FILLED
            if order.filled_shares == order.shares
            else OrderStatus.PARTIAL
        )
        return fill

    def _consume_fifo(self, asset_id: str, quantity: int, on: date) -> Decimal:
        remaining = quantity
        cost = Decimal()
        kept = []
        for lot in self.state.lots:
            if lot.asset_id != asset_id or lot.available_on > on or remaining == 0:
                kept.append(lot)
                continue
            used = min(lot.shares, remaining)
            cost += Decimal(used) * lot.unit_cost
            lot.shares -= used
            remaining -= used
            if lot.shares:
                kept.append(lot)
        if remaining:
            raise RuntimeError("sell quantity exceeded available lots")
        self.state.lots = kept
        return cost.quantize(Decimal("0.01"))

    def apply_action(self, action: CorporateAction, on: date) -> None:
        shares = self.state.shares(action.asset_id)
        if action.kind == "CASH_DIVIDEND":
            if on == action.ex_date:
                receipt = f"{action.action_id}:accrual"
                amount = (Decimal(shares) * action.cash_per_share).quantize(
                    Decimal("0.01")
                )
                if receipt not in self.state.applied_receipts:
                    self.state.receivables[action.action_id] = amount
                    self.state.post(
                        receipt,
                        on,
                        (
                            JournalLine("DIVIDEND_RECEIVABLE", debit=amount),
                            JournalLine("DIVIDEND_INCOME", credit=amount),
                        ),
                    )
            if action.pay_date and on == action.pay_date:
                receipt = f"{action.action_id}:payment"
                if receipt not in self.state.applied_receipts:
                    amount = self.state.receivables.pop(action.action_id, Decimal())
                    self.state.cash += amount
                    self.state.post(
                        receipt,
                        on,
                        (
                            JournalLine("CASH", debit=amount),
                            JournalLine("DIVIDEND_RECEIVABLE", credit=amount),
                        ),
                    )
        elif action.kind == "STOCK_DIVIDEND" and on == action.ex_date:
            receipt = f"{action.action_id}:stock"
            if receipt not in self.state.applied_receipts:
                for lot in self.state.lots:
                    if lot.asset_id == action.asset_id:
                        lot.shares += int(Decimal(lot.shares) * action.stock_ratio)
                self.state.post(receipt, on, ())
        else:
            if action.kind not in {"CASH_DIVIDEND", "STOCK_DIVIDEND"}:
                raise ValueError("unsupported corporate action")
