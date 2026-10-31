from __future__ import annotations
import hashlib
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from reference_engine.engine import RefAction, RefEngine, RefLot
from src.quantcore.accounting.ledger import AccountState, Lot
from src.quantcore.execution.formal import CorporateAction, FormalEngine
from src.quantcore.rules.rulebook import default_rulebook
from src.quantcore.validation.projection import formal_snapshot, reference_snapshot
from src.quantcore.validation.stage10 import compare_snapshots


def _next_session(on: date) -> date:
    calendar = {date(2024, 1, 5): date(2024, 1, 8), date(2024, 1, 8): date(2024, 1, 9)}
    if on not in calendar:
        raise ValueError("fixture calendar lacks next session")
    return calendar[on]


def deterministic_hand_replay():
    on = date(2024, 1, 5)
    pay = date(2024, 1, 8)
    formal_state = AccountState(
        Decimal("0"),
        [Lot("old", "asset", 100, Decimal("10"), date(2023, 1, 1), date(2023, 1, 2))],
    )
    formal = FormalEngine(formal_state, default_rulebook(), _next_session)
    reference = RefEngine(
        Decimal("0"),
        _next_session,
        [
            RefLot(
                "old", "asset", 100, Decimal("10"), date(2023, 1, 1), date(2023, 1, 2)
            )
        ],
    )
    formal.apply_action(
        CorporateAction("div", "asset", "CASH_DIVIDEND", on, pay, Decimal("1")), on
    )
    reference.apply_action(
        RefAction("div", "asset", "CASH_DIVIDEND", on, pay, Decimal("1")), on
    )
    first = (
        formal_snapshot(
            on,
            formal_state,
            [],
            formal.fills,
            {"asset": Decimal("9")},
            event_receipts=("div:accrual",),
        ),
    )
    second = (
        reference_snapshot(
            on,
            reference,
            [],
            reference.fills,
            {"asset": Decimal("9")},
            event_receipts=("div:accrual",),
        ),
    )
    input_hash = hashlib.sha256(b"s10-hand-fixture-v1").hexdigest()
    return compare_snapshots(first, second, input_hash=input_hash)


def known_real_readiness(control_database: Path) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{control_database.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT dataset,manifest_hash,readiness,provenance FROM data_manifests WHERE readiness='REAL_READY' AND provenance='REAL' AND (dataset LIKE '%price%' OR dataset LIKE '%daily%')"
        ).fetchall()
    finally:
        conn.close()
    return {
        "status": "READY" if rows else "BLOCKED_DATA",
        "reason": None
        if rows
        else "No published REAL_READY canonical price dataset is available for KNOWN replay.",
        "eligible_manifests": [list(x) for x in rows],
    }
