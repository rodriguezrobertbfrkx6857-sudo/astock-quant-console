from __future__ import annotations
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from src.quantcore.data.models import Readiness
from src.quantcore.research.oos import AccessDecision, AccessPurpose, AccessReceipt
from src.quantcore.research.queue import ResearchStore
from src.quantcore.research.registry import (
    QueueStatus,
    ResearchProtocol,
    Stage09Rating,
    StrategyVersion,
    build_purged_folds,
    evaluate_stage09,
    run_isolated_folds,
)

H = hashlib.sha256(b"x").hexdigest()


def protocol(control=False):
    return ResearchProtocol(
        "test.v1",
        "CONTROL" if control else "TEST",
        "hypothesis",
        "falsifier",
        H,
        (("window", (20, 60)),),
        "POSITIVE",
        "u1",
        ("prices",),
        "PIT",
        "NEXT_SESSION",
        "LONG_ONLY",
        "rules-v1",
        ("2018-01-01", "2020-12-31"),
        (
            ("2021-01-01", "2021-12-31"),
            ("2022-01-01", "2022-12-31"),
            ("2023-01-01", "2023-12-31"),
        ),
        5,
        5,
        1,
        ("CTRL",),
        "net_excess",
        "block-ci",
        2,
        "one-run",
        "KNOWN_ONLY",
        "INTERNAL",
        "v1",
        Decimal("0.02"),
        control,
    )


def receipt(decision=AccessDecision.ALLOW_KNOWN):
    return AccessReceipt(
        "r",
        decision,
        date(2021, 1, 1),
        date(2023, 12, 31),
        AccessPurpose.RESEARCH,
        datetime.now(UTC),
        "fixture",
    )


class S06ResearchTests(unittest.TestCase):
    def test_protocol_budget_purge_and_known_only_are_hard_gates(self):
        base = protocol()
        self.assertEqual(len(base.protocol_hash), 64)
        with self.assertRaises(ValueError):
            replace(base, purge_sessions=4)
        with self.assertRaises(ValueError):
            replace(base, oos_access="UNTOUCHED")
        with self.assertRaises(ValueError):
            replace(base, parameter_grid=(("x", tuple(range(3))),), family_budget=2)

    def test_purged_folds_have_no_label_overlap_and_reset_state(self):
        folds = build_purged_folds(100, 3, 5, 2)
        for fold in folds:
            self.assertLessEqual(
                max(fold.train_indices) + 5, min(fold.validation_indices)
            )
        created = []

        def factory():
            value = []
            created.append(value)
            return value

        results = run_isolated_folds(
            folds,
            factory,
            lambda state, fold: (id(state), len(fold.validation_indices)),
        )
        self.assertEqual(len({x[0] for x in results}), 3)
        with self.assertRaises(RuntimeError):
            shared = []
            run_isolated_folds(folds, lambda: shared, lambda state, fold: None)

    def test_stage09_separates_invalid_insufficient_reject_research_candidate(self):
        p = protocol()
        v = StrategyVersion(
            "v1",
            p.family_id,
            p.protocol_hash,
            p.source_code_hash,
            H,
            p.universe_version,
        )
        invalid = evaluate_stage09(
            p,
            v,
            readiness=Readiness.BLOCKED,
            access_receipt=receipt(),
            effective_trades=1,
            validation_blocks=3,
            primary_metric=Decimal("1"),
        )
        insufficient = evaluate_stage09(
            p,
            v,
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(),
            effective_trades=1,
            validation_blocks=2,
            primary_metric=Decimal("1"),
        )
        rejected = evaluate_stage09(
            p,
            v,
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(),
            effective_trades=0,
            validation_blocks=3,
            primary_metric=Decimal("1"),
        )
        research = evaluate_stage09(
            p,
            v,
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(),
            effective_trades=1,
            validation_blocks=3,
            primary_metric=Decimal("0.01"),
        )
        candidate = evaluate_stage09(
            p,
            v,
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(),
            effective_trades=1,
            validation_blocks=3,
            primary_metric=Decimal("0.02"),
        )
        self.assertEqual(
            [x.rating for x in (invalid, insufficient, rejected, research, candidate)],
            [
                Stage09Rating.INVALID,
                Stage09Rating.INSUFFICIENT_DATA,
                Stage09Rating.REJECT,
                Stage09Rating.RESEARCH,
                Stage09Rating.CANDIDATE,
            ],
        )
        self.assertFalse(candidate.paper_eligible)

    def test_control_and_denied_oos_never_candidate(self):
        p = protocol(True)
        v = StrategyVersion(
            "ctrl",
            p.family_id,
            p.protocol_hash,
            p.source_code_hash,
            H,
            p.universe_version,
        )
        control = evaluate_stage09(
            p,
            v,
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(),
            effective_trades=10,
            validation_blocks=3,
            primary_metric=Decimal("9"),
        )
        denied = evaluate_stage09(
            replace(p, control_only=False, family_id="TEST"),
            replace(
                v,
                family_id="TEST",
                protocol_hash=replace(
                    p, control_only=False, family_id="TEST"
                ).protocol_hash,
            ),
            readiness=Readiness.REAL_READY,
            access_receipt=receipt(AccessDecision.DENY_UNKNOWN),
            effective_trades=10,
            validation_blocks=3,
            primary_metric=Decimal("9"),
        )
        self.assertIs(control.rating, Stage09Rating.REJECT)
        self.assertIs(denied.rating, Stage09Rating.INVALID)

    def test_queue_success_is_not_research_approval(self):
        root = Path("state/v2/test_tmp")
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            db = Path(temp) / "control.sqlite"
            shutil.copy2("state/v2/control.sqlite", db)
            store = ResearchStore(db)
            p = protocol()
            v = StrategyVersion(
                "fixture-v",
                p.family_id,
                p.protocol_hash,
                p.source_code_hash,
                H,
                p.universe_version,
            )
            store.register(p, v, "fixture-q", 99)
            for status in (
                QueueStatus.READY,
                QueueStatus.RUNNING,
                QueueStatus.SUCCEEDED,
            ):
                store.transition("fixture-q", status)
            result = evaluate_stage09(
                p,
                v,
                readiness=Readiness.REAL_READY,
                access_receipt=receipt(),
                effective_trades=0,
                validation_blocks=3,
                primary_metric=Decimal("1"),
            )
            store.record_stage09("fixture-result", "fixture-q", result)
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM research_queue WHERE queue_item_id='fixture-q'"
                ).fetchone()[0],
                "SUCCEEDED",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT rating FROM stage09_results WHERE result_id='fixture-result'"
                ).fetchone()[0],
                "REJECT",
            )
            conn.close()
