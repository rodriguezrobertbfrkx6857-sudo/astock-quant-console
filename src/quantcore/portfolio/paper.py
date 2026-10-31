from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from src.quantcore.accounting.ledger import AccountState
from src.quantcore.accounting.storage import AccountingStore, OutboxMessage
from src.quantcore.execution.formal import Fill, FormalEngine, MarketState, Order
from src.quantcore.portfolio.qualification import QualificationService
from src.quantcore.rules.rulebook import default_rulebook
from src.quantcore.universe.models import Board, SecurityType


class RiskMode(StrEnum):
    NORMAL = "NORMAL"
    PAUSE_NEW_RISK = "PAUSE_NEW_RISK"
    EXIT_POSITIONS = "EXIT_POSITIONS"


@dataclass(frozen=True)
class PaperResult:
    account_id: str
    orders: tuple[Order, ...]
    new_fills: tuple[Fill, ...]
    blocked_orders: tuple[str, ...]


def aggregate_target_shares(
    strategy_targets: tuple[dict[str, int], ...], *, per_asset_cap: int
) -> dict[str, int]:
    if per_asset_cap <= 0:
        raise ValueError("per_asset_cap must be positive")
    totals: dict[str, int] = {}
    for targets in strategy_targets:
        for asset_id, shares in targets.items():
            if shares < 0:
                raise ValueError("long-only target shares cannot be negative")
            totals[asset_id] = min(per_asset_cap, totals.get(asset_id, 0) + shares)
    return totals


class PaperService:
    def __init__(
        self,
        control_database: str | Path,
        accounting_database: str | Path,
        next_session,
    ):
        self.qualifications = QualificationService(control_database)
        self.accounts = AccountingStore(accounting_database)
        self.next_session = next_session

    def ensure_empty_account(
        self, account_id: str, initial_cash: Decimal
    ) -> AccountState:
        return self.accounts.ensure_account(account_id, initial_cash)

    def execute_targets(
        self,
        *,
        account_id: str,
        qualification_id: str,
        snapshot_hash: str,
        on: date,
        targets: dict[str, int],
        boards: dict[str, Board],
        security_types: dict[str, SecurityType],
        prices: dict[str, Decimal],
        market_states: dict[str, MarketState],
        risk_mode: RiskMode = RiskMode.NORMAL,
        initial_cash: Decimal = Decimal("0"),
    ) -> PaperResult:
        self.qualifications.require_active(
            qualification_id, snapshot_hash=snapshot_hash
        )
        state = self.ensure_empty_account(account_id, initial_cash)
        current = {
            asset_id: state.shares(asset_id)
            for asset_id in set(x.asset_id for x in state.lots)
        }
        desired = (
            {asset_id: 0 for asset_id in current}
            if risk_mode is RiskMode.EXIT_POSITIONS
            else dict(targets)
        )
        orders = FormalEngine.plan_targets(current, desired, on, boards, security_types)
        if risk_mode is RiskMode.PAUSE_NEW_RISK:
            for order in orders:
                if order.side == "BUY":
                    order.status = type(order.status).BLOCKED
                    order.blocked_reason = "RISK_PAUSE_NEW_RISK"
        previous_orders = {x.order_id: x for x in self.accounts.load_orders(account_id)}
        engine = FormalEngine(state, default_rulebook(), self.next_session)
        new_fills: list[Fill] = []
        for order in orders:
            prior = previous_orders.get(order.order_id)
            if prior is not None and prior.status in {
                type(prior.status).FILLED,
                type(prior.status).PARTIAL,
            }:
                order.filled_shares, order.status, order.blocked_reason = (
                    prior.filled_shares,
                    prior.status,
                    prior.blocked_reason,
                )
                continue
            if order.status.value == "BLOCKED":
                continue
            fill = engine.execute(
                order, on, prices[order.asset_id], market=market_states[order.asset_id]
            )
            if fill is not None:
                new_fills.append(fill)
        outbox: tuple[OutboxMessage, ...] = ()
        if orders:
            event_payload: dict[str, object] = {
                "account_id": account_id,
                "qualification_id": qualification_id,
                "snapshot_hash": snapshot_hash,
                "business_date": on.isoformat(),
                "targets": targets,
                "risk_mode": risk_mode.value,
                "order_ids": [x.order_id for x in orders],
                "fill_ids": [x.fill_id for x in new_fills],
            }
            event_raw = json.dumps(
                event_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            business_event_id = hashlib.sha256(event_raw.encode()).hexdigest()
            outbox = (
                OutboxMessage(
                    f"paper:{business_event_id}",
                    business_event_id,
                    "PAPER_COMMITTED",
                    event_payload,
                ),
            )
        self.accounts.commit(
            account_id,
            state,
            orders=tuple(orders),
            fills=tuple(new_fills),
            outbox=outbox,
        )
        return PaperResult(
            account_id,
            tuple(orders),
            tuple(new_fills),
            tuple(x.order_id for x in orders if x.status.value == "BLOCKED"),
        )
