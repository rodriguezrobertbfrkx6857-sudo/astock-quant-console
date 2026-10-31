from __future__ import annotations

import json
import os
import sqlite3
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from src.quantcore.activation.intraday import IntradayStore, Tick, WatchEngine
from src.quantcore.activation.data_worker import (
    deduplicate_identical,
    normalize_optional_fields,
    sync_dataset,
)
from src.quantcore.activation.vendor import RELAY_BASE, RelayClient, VendorFailure
from src.quantcore.activation.operations import execute_one, register_daily
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.data.models import Provenance
from src.quantcore.runtime.jobs import JobSpec, RuntimeStore
from scripts.v2 import activation


class _Response:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class RelayActivationTests(unittest.TestCase):
    def test_normalization_only_collapses_identical_duplicates(self) -> None:
        rows = [
            {"k": "A", "v": 1},
            {"k": "A", "v": 1},
            {"k": "A", "v": 2},
        ]
        normalized, removed = deduplicate_identical(rows, ("k",))
        self.assertEqual(removed, 1)
        self.assertEqual(normalized, [{"k": "A", "v": 1}, {"k": "A", "v": 2}])

    def test_missing_optional_field_and_explicit_null_normalize_identically(
        self,
    ) -> None:
        rows = normalize_optional_fields([{"k": "A"}, {"k": "A", "optional": None}])
        normalized, removed = deduplicate_identical(rows, ("k",))
        self.assertEqual((len(normalized), removed), (1, 1))

    def test_missing_exact_environment_key_fails_closed(self) -> None:
        prior = os.environ.pop("TUSHARE_RELAY_KEY", None)
        try:
            with self.assertRaises(VendorFailure) as context:
                RelayClient(max_retries=0, sleeper=lambda _: None).fetch_page(
                    "daily", {"trade_date": "20260901"}
                )
            self.assertEqual(context.exception.code, "VENDOR_AUTH_REQUIRED")
        finally:
            if prior is not None:
                os.environ["TUSHARE_RELAY_KEY"] = prior

    def test_key_is_header_only_and_schema_is_strict(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["header"] = request.get_header("X-api-key")
            captured["timeout"] = timeout
            return _Response(
                {"code": 0, "data": {"fields": ["ts_code"], "items": [["000001.SZ"]]}}
            )

        with patch.dict(
            os.environ, {"TUSHARE_RELAY_KEY": "test-secret-never-persist"}, clear=False
        ):
            with patch("urllib.request.urlopen", fake_open):
                rows = RelayClient(max_retries=0, sleeper=lambda _: None).fetch_page(
                    "daily", {"trade_date": "20260901"}
                )
        self.assertEqual(rows, [{"ts_code": "000001.SZ"}])
        self.assertTrue(str(captured["url"]).startswith(RELAY_BASE))
        self.assertNotIn("test-secret-never-persist", str(captured["url"]))
        self.assertEqual(captured["header"], "test-secret-never-persist")

    def test_successful_vendor_page_persists_real_curated_manifest(self) -> None:
        cfg = load_v2_config()
        (cfg.paths.state / "test_tmp").mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(dir=cfg.paths.state / "test_tmp"))
        try:
            shutil.copy2(cfg.paths.state / "control.sqlite", temp / "control.sqlite")
            local_cfg = replace(
                cfg,
                paths=replace(
                    cfg.paths,
                    data=temp / "data",
                    state=temp,
                    artifacts=temp / "artifacts",
                    logs=temp / "logs",
                ),
            )
            page = [[{"ts_code": "000001.SZ", "trade_date": "20260908", "close": 10.0}]]
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                before = conn.execute(
                    "SELECT count(*) FROM data_manifests WHERE dataset='daily' "
                    "AND layer='CURATED' AND readiness='REAL_READY'"
                ).fetchone()[0]
            finally:
                conn.close()
            with patch.object(RelayClient, "fetch_pages", return_value=(page, True)):
                result = sync_dataset(
                    local_cfg, "daily", business_date="2026-09-08", publish=False
                )
            self.assertEqual(result["status"], "REAL_READY")
            self.assertFalse(result["publication_updated"])
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM data_manifests WHERE dataset='daily' AND layer='CURATED' AND readiness='REAL_READY'"
                    ).fetchone()[0],
                    before + 1,
                )
            finally:
                conn.close()
        finally:
            shutil.rmtree(temp)

    def test_partial_newer_cross_section_cannot_replace_publication(self) -> None:
        cfg = load_v2_config()
        (cfg.paths.state / "test_tmp").mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(dir=cfg.paths.state / "test_tmp"))
        try:
            shutil.copy2(cfg.paths.state / "control.sqlite", temp / "control.sqlite")
            local_cfg = replace(
                cfg,
                paths=replace(
                    cfg.paths,
                    data=temp / "data",
                    state=temp,
                    artifacts=temp / "artifacts",
                    logs=temp / "logs",
                ),
            )
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                before = conn.execute(
                    "SELECT manifest_id FROM data_publications WHERE dataset='daily'"
                ).fetchone()[0]
            finally:
                conn.close()
            page = [[{"ts_code": "000001.SZ", "trade_date": "20260908", "close": 10.0}]]
            with patch.object(RelayClient, "fetch_pages", return_value=(page, True)):
                result = sync_dataset(local_cfg, "daily", business_date="2026-09-08")
            self.assertEqual(result["status"], "BLOCKED_DATA")
            self.assertIn("CROSS_SESSION_COMPLETENESS_FAILED", result["publication_gate_reason"])
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                after = conn.execute(
                    "SELECT manifest_id FROM data_publications WHERE dataset='daily'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(after, before)
        finally:
            shutil.rmtree(temp)

    def test_preloaded_real_pages_preserve_read_only_source_identity(self) -> None:
        cfg = load_v2_config()
        (cfg.paths.state / "test_tmp").mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(dir=cfg.paths.state / "test_tmp"))
        try:
            shutil.copy2(cfg.paths.state / "control.sqlite", temp / "control.sqlite")
            local_cfg = replace(
                cfg,
                paths=replace(
                    cfg.paths,
                    data=temp / "data",
                    state=temp,
                    artifacts=temp / "artifacts",
                    logs=temp / "logs",
                ),
            )
            page = [[{"ts_code": "000001.SZ", "trade_date": "20260801", "close": 10.0}]]
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                prior_publication = conn.execute(
                    "SELECT manifest_id FROM data_publications WHERE dataset='daily'"
                ).fetchone()
            finally:
                conn.close()
            with patch.object(RelayClient, "fetch_pages") as fetch:
                result = sync_dataset(
                    local_cfg,
                    "daily",
                    business_date="2026-08-01",
                    preloaded_pages=page,
                    source_id="legacy-readonly.quant3.datahubco",
                    request_override={"trade_date": "20260801", "source_batch_id": "b1"},
                    publish=False,
                )
            fetch.assert_not_called()
            self.assertEqual(result["status"], "REAL_READY")
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                source = conn.execute(
                    "SELECT json_extract(manifest_json,'$.source_id') FROM data_manifests "
                    "WHERE manifest_hash=? AND layer='CURATED'",
                    (result["manifest_hash"],),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(source, "legacy-readonly.quant3.datahubco")
            conn = sqlite3.connect(temp / "control.sqlite")
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT manifest_id FROM data_publications WHERE dataset='daily'"
                    ).fetchone(),
                    prior_publication,
                )
            finally:
                conn.close()
        finally:
            shutil.rmtree(temp)


class IntradayActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_v2_config()
        (cfg.paths.state / "test_tmp").mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=cfg.paths.state / "test_tmp"))
        self.database = self.temp / "control.sqlite"
        shutil.copy2(cfg.paths.state / "control.sqlite", self.database)
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("DELETE FROM runtime_attempts")
            conn.execute("DELETE FROM runtime_dependencies")
            conn.execute("DELETE FROM runtime_jobs")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    @staticmethod
    def tick(
        price: str, observed: str, provenance: Provenance = Provenance.REAL
    ) -> Tick:
        return Tick(
            "relay",
            "510300.SH",
            observed,
            observed,
            Decimal(price),
            Decimal("10"),
            provenance,
            "a" * 64,
        )

    def test_only_real_ticks_enter_live_store_and_are_idempotent(self) -> None:
        store = IntradayStore(self.database)
        tick = self.tick("4.0", "2026-09-07T01:30:00+00:00")
        self.assertTrue(store.append(tick))
        self.assertFalse(store.append(tick))
        with self.assertRaises(ValueError):
            store.append(
                self.tick("4.1", "2026-09-07T01:31:00+00:00", Provenance.SYNTHETIC)
            )

    def test_watch_alerts_do_not_create_orders(self) -> None:
        prior = self.tick("4.0", "2026-09-07T01:30:00+00:00")
        current = self.tick("4.2", "2026-09-07T01:31:00+00:00")
        alerts = WatchEngine.evaluate(prior, current, volume_ratio=Decimal("3.2"))
        self.assertEqual(
            {x["type"] for x in alerts}, {"ABNORMAL_MOVE", "ABNORMAL_VOLUME"}
        )

    def test_runtime_waiting_data_is_explicit_and_not_reacquired(self) -> None:
        store = RuntimeStore(self.database)
        spec = JobSpec(
            "activation.fixture", date(2026, 9, 7), "snapshot", "v1", {}, "DAILY"
        )
        store.register(spec)
        lease = store.acquire("fixture-worker", loop_kind="DAILY")
        self.assertIsNotNone(lease)
        assert lease is not None
        store.defer(lease, status="WAITING_DATA", detail="vendor unavailable")
        self.assertIsNone(store.acquire("fixture-worker", loop_kind="DAILY"))

    def test_daily_data_job_uses_registered_business_date(self) -> None:
        cfg = replace(
            load_v2_config(),
            paths=replace(load_v2_config().paths, state=self.temp),
        )
        store = RuntimeStore(self.database)
        spec = JobSpec(
            "data.sync.daily",
            date(2026, 8, 4),
            "snapshot",
            "activation-test-v1",
            {"dataset": "daily"},
            "DAILY",
        )
        store.register(spec)
        with patch(
            "src.quantcore.activation.operations.sync_dataset",
            return_value={"status": "REAL_READY"},
        ) as sync:
            self.assertTrue(execute_one(cfg, "DAILY", "dated-worker"))
        sync.assert_called_once_with(cfg, "daily", business_date="2026-08-04")

    def test_optional_vendor_jobs_do_not_block_price_data_quality_dag(self) -> None:
        cfg = replace(
            load_v2_config(),
            paths=replace(load_v2_config().paths, state=self.temp),
        )
        keys = register_daily(cfg, date(2026, 9, 7))
        conn = sqlite3.connect(self.database)
        try:
            quality_key = conn.execute(
                "SELECT job_key FROM runtime_jobs WHERE job_type='daily.data_quality'"
            ).fetchone()[0]
            dependencies = {
                row[0]
                for row in conn.execute(
                    "SELECT j.job_type FROM runtime_dependencies d "
                    "JOIN runtime_jobs j ON j.job_key=d.depends_on_job_key "
                    "WHERE d.job_key=?",
                    (quality_key,),
                )
            }
        finally:
            conn.close()
        self.assertEqual(
            dependencies,
            {"data.sync.daily", "data.sync.adj_factor", "data.sync.stk_limit"},
        )
        self.assertEqual(len(keys), 14)

    def test_current_session_publication_is_reused_without_vendor_call(self) -> None:
        cfg = replace(
            load_v2_config(),
            paths=replace(load_v2_config().paths, state=self.temp),
        )
        conn = sqlite3.connect(self.database)
        try:
            current = conn.execute(
                "SELECT manifest_id,manifest_hash FROM data_manifests "
                "WHERE dataset='daily' AND layer='CURATED' AND readiness='REAL_READY' "
                "AND json_extract(manifest_json,'$.start_date')='20260907' "
                "AND json_extract(manifest_json,'$.end_date')='20260907' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(current)
            assert current is not None
            conn.execute(
                "UPDATE data_publications SET manifest_id=?,manifest_hash=? "
                "WHERE dataset='daily'",
                current,
            )
            conn.commit()
        finally:
            conn.close()
        store = RuntimeStore(self.database)
        spec = JobSpec(
            "data.sync.daily",
            date(2026, 9, 7),
            "snapshot",
            "activation-test-reuse-v1",
            {"dataset": "daily"},
            "DAILY",
        )
        key = store.register(spec)
        with patch("src.quantcore.activation.operations.sync_dataset") as sync:
            self.assertTrue(execute_one(cfg, "DAILY", "reuse-worker"))
        sync.assert_not_called()
        conn = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM runtime_jobs WHERE job_key=?", (key,)
                ).fetchone()[0],
                "SUCCEEDED",
            )
        finally:
            conn.close()

    def test_universe_stage_defers_when_historical_state_is_absent(self) -> None:
        cfg = replace(
            load_v2_config(),
            paths=replace(load_v2_config().paths, state=self.temp),
        )
        store = RuntimeStore(self.database)
        spec = JobSpec(
            "daily.universe",
            date(2026, 9, 7),
            "snapshot",
            "activation-test-universe-v1",
            {"stage": "universe"},
            "DAILY",
        )
        key = store.register(spec)
        self.assertTrue(execute_one(cfg, "DAILY", "universe-worker"))
        conn = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM runtime_jobs WHERE job_key=?", (key,)
                ).fetchone()[0],
                "WAITING_DATA",
            )
        finally:
            conn.close()

    def test_market_hours_watch_degradation_uses_valid_persisted_status(self) -> None:
        cfg = replace(
            load_v2_config(),
            paths=replace(load_v2_config().paths, state=self.temp),
        )

        class _Status:
            watch_enabled = True

            @staticmethod
            def as_dict() -> dict[str, object]:
                return {
                    "phase": "MARKET",
                    "watch_enabled": True,
                    "calendar": {
                        "known": True,
                        "source": "PUBLISHED_TRADE_CAL",
                    },
                }

        with (
            patch.object(activation.RelayClient, "configured", return_value=True),
            patch.object(activation, "status_at", return_value=_Status()),
            patch.object(activation, "_write_supported"),
        ):
            payload = activation._update_scheduler_status(cfg)
        self.assertEqual(payload["scheduler_status"], "WATCH_DEGRADED_VENDOR")
        conn = sqlite3.connect(self.database)
        try:
            persisted = conn.execute(
                "SELECT status FROM operational_status WHERE component='scheduler'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(persisted, "DEGRADED")


if __name__ == "__main__":
    unittest.main()
