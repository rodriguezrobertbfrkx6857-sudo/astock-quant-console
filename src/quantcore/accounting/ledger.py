from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import uuid4


@dataclass(frozen=True)
class JournalLine:
    account: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    event_id: str
    on: date
    lines: tuple[JournalLine, ...]

    def __post_init__(self):
        if sum((x.debit for x in self.lines), Decimal()) != sum(
            (x.credit for x in self.lines), Decimal()
        ):
            raise ValueError("unbalanced journal entry")


@dataclass
class Lot:
    lot_id: str
    asset_id: str
    shares: int
    unit_cost: Decimal
    acquired_on: date
    available_on: date


@dataclass
class AccountState:
    cash: Decimal
    lots: list[Lot] = field(default_factory=list)
    receivables: dict[str, Decimal] = field(default_factory=dict)
    journal: list[JournalEntry] = field(default_factory=list)
    applied_receipts: set[str] = field(default_factory=set)

    def shares(self, asset_id: str) -> int:
        return sum(x.shares for x in self.lots if x.asset_id == asset_id)

    def available_shares(self, asset_id: str, on: date) -> int:
        return sum(
            x.shares
            for x in self.lots
            if x.asset_id == asset_id and x.available_on <= on
        )

    def post(self, event_id: str, on: date, lines: tuple[JournalLine, ...]) -> None:
        if event_id in self.applied_receipts:
            return
        self.journal.append(JournalEntry(f"journal-{uuid4().hex}", event_id, on, lines))
        self.applied_receipts.add(event_id)

    def nav(self, prices: dict[str, Decimal]) -> Decimal:
        return (
            self.cash
            + sum(self.receivables.values(), Decimal())
            + sum(Decimal(x.shares) * prices[x.asset_id] for x in self.lots)
        )
