from __future__ import annotations
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from src.quantcore.accounting.ledger import AccountState
from src.quantcore.execution.formal import MarketState
from src.quantcore.portfolio.paper import (
    PaperService,
    RiskMode,
    aggregate_target_shares,
)
from src.quantcore.portfolio.qualification import QualificationService
from src.quantcore.universe.models import Board, SecurityType

D = Decimal
H = hashlib.sha256(b"snapshot").hexdigest()
P = hashlib.sha256(b"protocol").hexdigest()
F = hashlib.sha256(b"formal").hexdigest()
R = hashlib.sha256(b"reference").hexdigest()


def next_session(on):
    return on + timedelta(days=1)


class S07PaperTests(unittest.TestCase):
    def setUp(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir="state/v2/test_tmp")
        self.control = Path(self.temp.name) / "control.sqlite"
        self.accounting = Path(self.temp.name) / "accounting.sqlite"
        shutil.copy2("state/v2/control.sqlite", self.control)
        shutil.copy2("state/v2/accounting.sqlite", self.accounting)
        conn = sqlite3.connect(self.control)
        now = "2026-01-01T00:00:00Z"
        conn.execute("INSERT INTO strategy_families VALUES ('PAPER_FIXTURE',0)")
        conn.execute(
            "INSERT INTO research_protocols VALUES (?,?,?,?,?)",
            (P, "paper.fixture", "PAPER_FIXTURE", "{}", now),
        )
        conn.execute(
            "INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?)",
            (
                "paper-v1",
                "PAPER_FIXTURE",
                P,
                hashlib.sha256(b"code").hexdigest(),
                H,
                "u1",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO research_queue VALUES (?,?,?,?,?,?,?,?)",
            ("paper-q", "paper-v1", P, 99, "SUCCEEDED", 1, now, now),
        )
        conn.execute(
            "INSERT INTO stage09_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("paper-s09", "paper-q", P, H, "CANDIDATE", "[]", "1", 1, 3, now),
        )
        conn.execute(
            "INSERT INTO stage10_runs VALUES (?,?,?,?,?,?,?,?)",
            ("paper-s10", "paper-s09", H, F, R, "PASS", 1, now),
        )
        conn.commit()
        conn.close()
        self.qualification = QualificationService(self.control).issue(
            "paper-v1", "paper-s09", "paper-s10"
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self):
        return PaperService(self.control, self.accounting, next_session)

    def test_qualification_requires_exact_evidence_and_can_be_revoked(self):
        self.assertEqual(self.qualification.status, "ACTIVE")
        q = QualificationService(self.control)
        q.revoke(self.qualification.qualification_id, "data revision")
        with self.assertRaises(PermissionError):
            q.require_active(self.qualification.qualification_id, snapshot_hash=H)
        with self.assertRaises(PermissionError):
            q.require_active(
                self.qualification.qualification_id,
                snapshot_hash=hashlib.sha256(b"changed").hexdigest(),
            )

    def test_paper_buy_repeat_is_idempotent_and_lot_settles_t1(self):
        svc = self.service()
        conn = sqlite3.connect(self.accounting)
        before_outbox = conn.execute(
            "SELECT count(*) FROM accounting_outbox WHERE topic='PAPER_COMMITTED'"
        ).fetchone()[0]
        conn.close()
        kwargs = dict(
            account_id="paper",
            qualification_id=self.qualification.qualification_id,
            snapshot_hash=H,
            on=date(2024, 1, 2),
            targets={"asset": 100},
            boards={"asset": Board.SSE_MAIN},
            security_types={"asset": SecurityType.STOCK},
            prices={"asset": D("10")},
            market_states={"asset": MarketState(True, D("11"), D("9"), "fixture")},
            initial_cash=D("10000"),
        )
        first = svc.execute_targets(**kwargs)
        second = svc.execute_targets(**kwargs)
        self.assertEqual(len(first.new_fills), 1)
        self.assertEqual(len(second.new_fills), 0)
        state = svc.accounts.load("paper")
        self.assertEqual(state.shares("asset"), 100)
        self.assertEqual(state.available_shares("asset", date(2024, 1, 2)), 0)
        self.assertEqual(len(svc.accounts.load_fills("paper")), 1)
        conn = sqlite3.connect(self.accounting)
        self.assertEqual(
            conn.execute(
                "SELECT count(*) FROM accounting_outbox WHERE topic='PAPER_COMMITTED'"
            ).fetchone()[0],
            before_outbox + 1,
        )
        conn.close()

    def test_pause_blocks_new_risk_but_exit_sells_existing_position(self):
        svc = self.service()
        common = dict(
            account_id="risk",
            qualification_id=self.qualification.qualification_id,
            snapshot_hash=H,
            boards={"asset": Board.SSE_MAIN},
            security_types={"asset": SecurityType.STOCK},
            prices={"asset": D("10")},
            market_states={"asset": MarketState(True, D("11"), D("9"), "fixture")},
            initial_cash=D("10000"),
        )
        paused = svc.execute_targets(
            on=date(2024, 1, 2),
            targets={"asset": 100},
            risk_mode=RiskMode.PAUSE_NEW_RISK,
            **common,
        )
        self.assertEqual(len(paused.new_fills), 0)
        svc.execute_targets(on=date(2024, 1, 2), targets={"asset": 100}, **common)
        exited = svc.execute_targets(
            on=date(2024, 1, 3),
            targets={"asset": 100},
            risk_mode=RiskMode.EXIT_POSITIONS,
            **common,
        )
        self.assertEqual(exited.new_fills[0].side, "SELL")
        self.assertEqual(svc.accounts.load("risk").shares("asset"), 0)

    def test_empty_account_and_portfolio_cap_are_valid(self):
        state = self.service().ensure_empty_account("empty", D("1000"))
        self.assertEqual(state.lots, [])
        self.assertEqual(
            aggregate_target_shares(({"a": 80}, {"a": 50, "b": 10}), per_asset_cap=100),
            {"a": 100, "b": 10},
        )

    def test_account_initialization_retry_does_not_reset_existing_ledger(self):
        svc = self.service()
        svc.ensure_empty_account("retry", D("1000"))
        svc.accounts.commit("retry", AccountState(D("321")))

        retried = svc.ensure_empty_account("retry", D("1000"))

        self.assertEqual(retried.cash, D("321"))

    def test_non_candidate_cannot_be_qualified(self):
        conn = sqlite3.connect(self.control)
        conn.execute(
            "INSERT INTO stage09_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "reject-s09",
                "paper-q",
                P,
                hashlib.sha256(b"other").hexdigest(),
                "REJECT",
                "[]",
                "-1",
                1,
                3,
                "2026-01-02T00:00:00Z",
            ),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            QualificationService(self.control).issue(
                "paper-v1", "reject-s09", "paper-s10"
            )
