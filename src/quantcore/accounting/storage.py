from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from src.quantcore.accounting.ledger import AccountState, JournalEntry, JournalLine, Lot
from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.execution.formal import Fill, Order, OrderStatus
from src.quantcore.universe.models import Board, SecurityType


class PersistenceConflict(RuntimeError):
    """Raised when an immutable receipt is reused with different content."""


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    business_event_id: str
    topic: str
    payload: dict[str, object]


def _text(value: Decimal) -> str:
    return format(value, "f")


class AccountingStore:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)
        if not self.database.is_file():
            raise RuntimeError("versioned accounting database is required")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _immutable_insert(
        conn: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        key_where: str,
        key: tuple[object, ...],
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        row = conn.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE {key_where}", key
        ).fetchone()
        if row is None or tuple(row) != tuple(values):
            raise PersistenceConflict(f"immutable {table} receipt conflict")

    def commit(
        self,
        account_id: str,
        state: AccountState,
        *,
        orders: tuple[Order, ...] = (),
        fills: tuple[Fill, ...] = (),
        outbox: tuple[OutboxMessage, ...] = (),
    ) -> None:
        if not account_id:
            raise ValueError("account_id is required")
        now = datetime.now(UTC).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO accounts(account_id,cash,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET cash=excluded.cash,updated_at=excluded.updated_at",
                (account_id, _text(state.cash), now),
            )
            for order in orders:
                immutable = (
                    account_id,
                    order.order_id,
                    order.asset_id,
                    order.side,
                    order.shares,
                    order.created_on.isoformat(),
                    order.board.value,
                    order.security_type.value,
                )
                previous = conn.execute(
                    "SELECT account_id,order_id,asset_id,side,shares,created_on,board,security_type FROM orders WHERE account_id=? AND order_id=?",
                    (account_id, order.order_id),
                ).fetchone()
                if previous is not None and tuple(previous) != immutable:
                    raise PersistenceConflict("order identity conflict")
                conn.execute(
                    "INSERT INTO orders(account_id,order_id,asset_id,side,shares,created_on,board,security_type,filled_shares,status,blocked_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,order_id) DO UPDATE SET "
                    "filled_shares=excluded.filled_shares,status=excluded.status,blocked_reason=excluded.blocked_reason",
                    immutable
                    + (order.filled_shares, order.status.value, order.blocked_reason),
                )
            for fill in fills:
                columns: tuple[str, ...] = (
                    "account_id",
                    "fill_id",
                    "order_id",
                    "asset_id",
                    "side",
                    "trade_on",
                    "shares",
                    "price",
                    "gross",
                    "fee",
                    "rule_id",
                )
                values: tuple[object, ...] = (
                    account_id,
                    fill.fill_id,
                    fill.order_id,
                    fill.asset_id,
                    fill.side,
                    fill.on.isoformat(),
                    fill.shares,
                    _text(fill.price),
                    _text(fill.gross),
                    _text(fill.fee),
                    fill.rule_id,
                )
                self._immutable_insert(
                    conn,
                    "fills",
                    columns,
                    values,
                    "account_id=? AND fill_id=?",
                    (account_id, fill.fill_id),
                )
            for entry in state.journal:
                columns = ("account_id", "entry_id", "event_id", "event_on")
                values = (
                    account_id,
                    entry.entry_id,
                    entry.event_id,
                    entry.on.isoformat(),
                )
                self._immutable_insert(
                    conn,
                    "journal_entries",
                    columns,
                    values,
                    "account_id=? AND entry_id=?",
                    (account_id, entry.entry_id),
                )
                for line_no, line in enumerate(entry.lines):
                    line_columns = (
                        "account_id",
                        "entry_id",
                        "line_no",
                        "ledger_account",
                        "debit",
                        "credit",
                    )
                    line_values = (
                        account_id,
                        entry.entry_id,
                        line_no,
                        line.account,
                        _text(line.debit),
                        _text(line.credit),
                    )
                    self._immutable_insert(
                        conn,
                        "journal_lines",
                        line_columns,
                        line_values,
                        "account_id=? AND entry_id=? AND line_no=?",
                        (account_id, entry.entry_id, line_no),
                    )
            for receipt in state.applied_receipts:
                conn.execute(
                    "INSERT OR IGNORE INTO corporate_action_receipts(account_id,receipt_id,applied_at) VALUES (?,?,?)",
                    (account_id, receipt, now),
                )
            conn.execute("DELETE FROM lots WHERE account_id=?", (account_id,))
            conn.executemany(
                "INSERT INTO lots(account_id,lot_id,asset_id,shares,unit_cost,acquired_on,available_on) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        account_id,
                        x.lot_id,
                        x.asset_id,
                        x.shares,
                        _text(x.unit_cost),
                        x.acquired_on.isoformat(),
                        x.available_on.isoformat(),
                    )
                    for x in state.lots
                ],
            )
            conn.execute("DELETE FROM receivables WHERE account_id=?", (account_id,))
            conn.executemany(
                "INSERT INTO receivables(account_id,action_id,amount) VALUES (?,?,?)",
                [
                    (account_id, key, _text(value))
                    for key, value in state.receivables.items()
                ],
            )
            for message in outbox:
                payload_json = json.dumps(
                    message.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
                event_values = (
                    message.business_event_id,
                    account_id,
                    message.topic,
                    payload_hash,
                )
                previous_event = conn.execute(
                    "SELECT business_event_id,account_id,event_type,payload_hash FROM business_events WHERE business_event_id=?",
                    (message.business_event_id,),
                ).fetchone()
                if previous_event is not None and tuple(previous_event) != event_values:
                    raise PersistenceConflict("business event identity conflict")
                conn.execute(
                    "INSERT OR IGNORE INTO business_events VALUES (?,?,?,?,?)",
                    event_values + (now,),
                )
                columns = (
                    "message_id",
                    "business_event_id",
                    "topic",
                    "payload_hash",
                    "payload_json",
                    "created_at",
                    "delivered_at",
                )
                values = (
                    message.message_id,
                    message.business_event_id,
                    message.topic,
                    payload_hash,
                    payload_json,
                    now,
                    None,
                )
                existing = conn.execute(
                    "SELECT message_id,business_event_id,topic,payload_hash,payload_json,created_at,delivered_at FROM accounting_outbox WHERE message_id=?",
                    (message.message_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO accounting_outbox VALUES (?,?,?,?,?,?,?)", values
                    )
                elif tuple(existing)[0:5] != values[0:5]:
                    raise PersistenceConflict("outbox message identity conflict")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_account(self, account_id: str, initial_cash: Decimal) -> AccountState:
        """Create an empty account exactly once, then return its state.

        Account binding and ledger initialization are separate durable stores.
        A retry after a process interruption must therefore use an atomic
        insert-if-absent rather than ``commit(AccountState(initial_cash))``;
        the latter would reset cash and rewrite the account after fills or NAV
        already exist.
        """

        if not account_id:
            raise ValueError("account_id is required")
        now = datetime.now(UTC).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO accounts(account_id,cash,updated_at) VALUES (?,?,?)",
                (account_id, _text(initial_cash), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.load(account_id)

    def load(self, account_id: str) -> AccountState:
        conn = self._connect()
        try:
            account = conn.execute(
                "SELECT cash FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise KeyError(account_id)
            lots = [
                Lot(
                    row["lot_id"],
                    row["asset_id"],
                    row["shares"],
                    Decimal(row["unit_cost"]),
                    date.fromisoformat(row["acquired_on"]),
                    date.fromisoformat(row["available_on"]),
                )
                for row in conn.execute(
                    "SELECT * FROM lots WHERE account_id=? ORDER BY lot_id",
                    (account_id,),
                )
            ]
            receivables = {
                row["action_id"]: Decimal(row["amount"])
                for row in conn.execute(
                    "SELECT action_id,amount FROM receivables WHERE account_id=?",
                    (account_id,),
                )
            }
            journal: list[JournalEntry] = []
            for row in conn.execute(
                "SELECT entry_id,event_id,event_on FROM journal_entries WHERE account_id=? ORDER BY rowid",
                (account_id,),
            ):
                lines = tuple(
                    JournalLine(
                        x["ledger_account"], Decimal(x["debit"]), Decimal(x["credit"])
                    )
                    for x in conn.execute(
                        "SELECT ledger_account,debit,credit FROM journal_lines WHERE account_id=? AND entry_id=? ORDER BY line_no",
                        (account_id, row["entry_id"]),
                    )
                )
                journal.append(
                    JournalEntry(
                        row["entry_id"],
                        row["event_id"],
                        date.fromisoformat(row["event_on"]),
                        lines,
                    )
                )
            receipts = {
                row[0]
                for row in conn.execute(
                    "SELECT receipt_id FROM corporate_action_receipts WHERE account_id=?",
                    (account_id,),
                )
            }
            receipts.update(entry.event_id for entry in journal)
            return AccountState(
                Decimal(account["cash"]), lots, receivables, journal, receipts
            )
        finally:
            conn.close()

    def load_orders(self, account_id: str) -> list[Order]:
        conn = self._connect()
        try:
            return [
                Order(
                    row["order_id"],
                    row["asset_id"],
                    row["side"],
                    row["shares"],
                    date.fromisoformat(row["created_on"]),
                    Board(row["board"]),
                    SecurityType(row["security_type"]),
                    row["filled_shares"],
                    OrderStatus(row["status"]),
                    row["blocked_reason"],
                )
                for row in conn.execute(
                    "SELECT * FROM orders WHERE account_id=? ORDER BY order_id",
                    (account_id,),
                )
            ]
        finally:
            conn.close()

    def load_fills(self, account_id: str) -> list[Fill]:
        conn = self._connect()
        try:
            return [
                Fill(
                    row["fill_id"],
                    row["order_id"],
                    row["asset_id"],
                    row["side"],
                    date.fromisoformat(row["trade_on"]),
                    row["shares"],
                    Decimal(row["price"]),
                    Decimal(row["gross"]),
                    Decimal(row["fee"]),
                    row["rule_id"],
                )
                for row in conn.execute(
                    "SELECT * FROM fills WHERE account_id=? ORDER BY trade_on,fill_id",
                    (account_id,),
                )
            ]
        finally:
            conn.close()

    def record_nav(
        self,
        account_id: str,
        nav_on: date,
        nav: Decimal,
        price_snapshot_hash: str,
        run_id: str,
    ) -> None:
        """Append one immutable PAPER valuation receipt.

        The run id is part of the primary key so a restarted worker can
        replay the same business event and verify identity instead of
        creating a second valuation row.
        """

        if nav <= 0 or not price_snapshot_hash or not run_id:
            raise ValueError("valid NAV, price snapshot and run id are required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            values = (
                account_id,
                nav_on.isoformat(),
                _text(nav),
                price_snapshot_hash,
                run_id,
            )
            existing = conn.execute(
                "SELECT account_id,nav_on,nav,price_snapshot_hash,run_id "
                "FROM daily_nav WHERE account_id=? AND nav_on=? AND run_id=?",
                (account_id, nav_on.isoformat(), run_id),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise PersistenceConflict("daily NAV identity conflict")
            conn.execute(
                "INSERT OR IGNORE INTO daily_nav(account_id,nav_on,nav,price_snapshot_hash,run_id) VALUES (?,?,?,?,?)",
                values,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
