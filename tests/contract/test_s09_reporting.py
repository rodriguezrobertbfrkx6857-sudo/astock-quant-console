from __future__ import annotations
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from src.quantcore.reporting.metrics import MetricCalculator, ReportingStore
from quantweb import v2_api

D = Decimal


class S09ReportingTests(unittest.TestCase):
    def test_metrics_are_calculated_once_with_one_named_convention(self):
        metrics = MetricCalculator().nav_metrics(
            (
                ("2024-01-01", D("100")),
                ("2024-01-02", D("110")),
                ("2024-01-03", D("99")),
            )
        )
        by_id = {x.metric_id: x for x in metrics}
        self.assertEqual(by_id["total_return"].value, D("-0.01"))
        self.assertEqual(by_id["max_drawdown"].value, D("-0.1"))
        self.assertTrue(all("ANNUALIZATION=252" in x.convention for x in metrics))
        self.assertTrue(all(x.source_hash == metrics[0].source_hash for x in metrics))

    def test_insufficient_series_is_explicit_null_not_zero(self):
        metrics = MetricCalculator().nav_metrics((("2024-01-01", D("100")),))
        self.assertTrue(
            all(x.value is None and x.display_value == "—" for x in metrics)
        )

    def test_reporting_publication_rejects_non_real_or_failed_quality(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="state/v2/test_tmp") as temp:
            db = Path(temp) / "control.sqlite"
            shutil.copy2("state/v2/control.sqlite", db)
            store = ReportingStore(db)
            with self.assertRaises(ValueError):
                store.publish(
                    "bad",
                    date(2024, 1, 1),
                    "SYNTHETIC",
                    "PASS",
                    hashlib.sha256(b"i").hexdigest(),
                    "run",
                    {},
                    "e",
                )

    def test_identical_reporting_publication_is_idempotent(self):
        Path("state/v2/test_tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="state/v2/test_tmp") as temp:
            db = Path(temp) / "control.sqlite"
            shutil.copy2("state/v2/control.sqlite", db)
            store = ReportingStore(db)
            source_hash = hashlib.sha256(b"nav").hexdigest()
            metrics = MetricCalculator().nav_metrics(
                (("2024-01-01", D("100")), ("2024-01-02", D("101")))
            )
            args = (
                "idempotent-publication",
                date(2024, 1, 2),
                "REAL",
                "PASS",
                source_hash,
                "run",
                {("PORTFOLIO", "fixture"): metrics},
                "evidence",
            )
            store.publish(*args)
            store.publish(*args)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM reporting_publications "
                        "WHERE publication_id='idempotent-publication'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_v2_empty_state_is_honest_and_all_envelopes_are_versioned(self):
        for response in (
            v2_api.overview(),
            v2_api.factors(),
            v2_api.strategies(),
            v2_api.backtests(),
            v2_api.data_health(),
            v2_api.system(),
            v2_api.events(),
            v2_api.paper(),
        ):
            self.assertEqual(response["schema_version"], 2)
            self.assertIn("publication_id", response)
            self.assertIn("as_of", response)
            self.assertIn("provenance", response)
            self.assertIn("quality_status", response)
            self.assertIn("stale", response)
            self.assertIn("evidence_refs", response)
        overview = v2_api.overview()
        if overview["publication_id"] == "NO_PUBLICATION":
            self.assertTrue(overview["stale"])
        else:
            self.assertEqual(overview["provenance"], "REAL")
            self.assertEqual(overview["quality_status"], "PASS")
        self.assertIn("module_status", overview)
        paper_status = overview["module_status"]["PAPER"]
        self.assertIsNone(paper_status["latest_execution_date"])
        self.assertEqual(paper_status["status"], "PREPARED_WAITING_FIRST_SESSION")
        self.assertTrue(paper_status["usable"])
        self.assertEqual(paper_status["scope"], "ETF3_FORWARD_CURRENT")
        self.assertEqual(
            paper_status["data_date"], overview["current_data_scope"]["latest_complete_eod"]
        )
