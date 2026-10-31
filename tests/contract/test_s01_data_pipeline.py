from __future__ import annotations

import json
import shutil
import unittest
import uuid

from src.quantcore.contracts.safety import PROJECT_ROOT
from src.quantcore.data.manifest import ManifestStore, derive_readiness
from src.quantcore.data.models import (
    AttemptStatus,
    DataLayer,
    DataManifest,
    Provenance,
    QualityStatus,
    Readiness,
)
from src.quantcore.data.normalize import normalize_attempt
from src.quantcore.data.publish import PublicationStore
from src.quantcore.data.quality import evaluate_records
from src.quantcore.data.raw import RawStore


class CanonicalDataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "state" / "v2" / "test_tmp" / uuid.uuid4().hex
        self.raw = RawStore(self.root / "raw")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _success(self, provenance: Provenance = Provenance.REAL):
        return self.raw.capture_json_pages(
            source_id="fixture-source",
            dataset="daily",
            request={"trade_date": "20260901", "token": "never-print-this"},
            pages=[[{"symbol": "000001.SZ", "trade_date": "20260901", "close": 10.0}]],
            provenance=provenance,
            pages_expected=1,
        )

    def _manifest(
        self, attempt, provenance=Provenance.REAL, quality=QualityStatus.PASS
    ):
        readiness, reasons = derive_readiness(
            provenance=provenance,
            attempt_status=attempt.status,
            row_count=attempt.row_count,
            pagination_complete=attempt.pages_expected == attempt.pages_received,
            quality_status=quality,
        )
        return DataManifest(
            manifest_id=f"manifest-{uuid.uuid4().hex}",
            dataset="daily",
            version="s01-fixture-v1",
            layer=DataLayer.RAW,
            provenance=provenance,
            source_id=attempt.source_id,
            created_at=attempt.completed_at,
            available_at_policy="fixture-known-at-close",
            schema_version="daily.v1",
            normalizer_version=None,
            build_hash="fixture-build",
            object_hashes=attempt.object_hashes,
            upstream_manifest_ids=(),
            row_count=attempt.row_count,
            symbol_count=1 if attempt.row_count else 0,
            start_date="2026-09-01" if attempt.row_count else None,
            end_date="2026-09-01" if attempt.row_count else None,
            pages_expected=attempt.pages_expected,
            pages_received=attempt.pages_received,
            attempt_status=attempt.status,
            quality_report_id="quality-fixture",
            quality_status=quality,
            readiness=readiness,
            reasons=reasons,
        )

    def test_success_is_content_addressed_and_request_is_redacted(self) -> None:
        attempt = self._success()
        self.assertIs(attempt.status, AttemptStatus.SUCCESS)
        stored = json.loads(
            (self.raw.attempts / f"{attempt.attempt_id}.json").read_text("utf-8")
        )
        self.assertEqual(stored["request"]["token"], "<redacted>")
        digest = attempt.object_hashes[0]
        self.assertEqual(self.raw.put_object(self.raw.read_object(digest)), digest)

    def test_failed_empty_and_partial_never_become_success(self) -> None:
        failed = self.raw.capture_json_pages(
            source_id="fixture",
            dataset="daily",
            request={},
            pages=[],
            provenance=Provenance.REAL,
            pages_expected=1,
            error_code="HTTP_503",
        )
        empty = self.raw.capture_json_pages(
            source_id="fixture",
            dataset="daily",
            request={},
            pages=[[]],
            provenance=Provenance.REAL,
            pages_expected=1,
        )
        partial = self.raw.capture_json_pages(
            source_id="fixture",
            dataset="daily",
            request={},
            pages=[[{"x": 1}]],
            provenance=Provenance.REAL,
            pages_expected=2,
        )
        self.assertEqual(
            (failed.status, empty.status, partial.status),
            (AttemptStatus.FAILED, AttemptStatus.EMPTY, AttemptStatus.PARTIAL),
        )
        self.assertFalse(failed.object_hashes)
        for item in (failed, empty, partial):
            readiness, _ = derive_readiness(
                provenance=item.provenance,
                attempt_status=item.status,
                row_count=item.row_count,
                pagination_complete=item.pages_expected == item.pages_received,
                quality_status=QualityStatus.PASS,
            )
            self.assertIs(readiness, Readiness.BLOCKED)
        with self.assertRaises(ValueError):
            normalize_attempt(self.raw, partial, lambda row: row)

    def test_quality_failures_are_not_promotable(self) -> None:
        report = evaluate_records(
            [{"symbol": "A", "date": "1"}, {"symbol": "A", "date": "1"}],
            required_fields=("symbol", "date", "close"),
            unique_fields=("symbol", "date"),
        )
        self.assertIs(report.status, QualityStatus.FAIL)

    def test_manifest_hash_detects_one_byte_change(self) -> None:
        attempt = self._success()
        store = ManifestStore(self.root / "manifests", self.raw.objects)
        frozen = store.freeze(self._manifest(attempt))
        path = store.root / f"{frozen.manifest_id}.json"
        body = bytearray(path.read_bytes())
        body[10] = ord("X") if body[10] != ord("X") else ord("Y")
        path.write_bytes(body)
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            store.load(frozen.manifest_id)

    def test_manifest_verification_detects_one_byte_object_change(self) -> None:
        attempt = self._success()
        store = ManifestStore(self.root / "manifests", self.raw.objects)
        frozen = store.freeze(self._manifest(attempt))
        digest = attempt.object_hashes[0]
        object_path = self.raw.objects / digest[:2] / digest
        body = bytearray(object_path.read_bytes())
        body[-1] = body[-1] ^ 1
        object_path.write_bytes(body)
        with self.assertRaises(ValueError):
            store.load(frozen.manifest_id)

    def test_normalization_is_deterministic_and_version_external(self) -> None:
        attempt = self._success()

        def normalizer(row):
            return {
                "asset_id": row["symbol"],
                "date": row["trade_date"],
                "raw_close": row["close"],
            }

        first, rows = normalize_attempt(self.raw, attempt, normalizer)
        second, _ = normalize_attempt(self.raw, attempt, normalizer)
        self.assertEqual(first, second)
        self.assertEqual(rows[0]["raw_close"], 10.0)

    def test_only_complete_real_quality_pass_manifest_publishes(self) -> None:
        attempt = self._success()
        store = ManifestStore(self.root / "manifests", self.raw.objects)
        publication = PublicationStore(self.root / "published", store)
        real = store.freeze(self._manifest(attempt))
        self.assertTrue(publication.publish("daily", real.manifest_id).is_file())

        synthetic_attempt = self._success(Provenance.SYNTHETIC)
        synthetic = store.freeze(
            self._manifest(synthetic_attempt, Provenance.SYNTHETIC)
        )
        self.assertIs(synthetic.readiness, Readiness.RESEARCH_ONLY)
        with self.assertRaises(ValueError):
            publication.publish("daily", synthetic.manifest_id)

    def test_unknown_and_mixed_are_blocked(self) -> None:
        for provenance in (Provenance.UNKNOWN, Provenance.MIXED):
            readiness, reasons = derive_readiness(
                provenance=provenance,
                attempt_status=AttemptStatus.SUCCESS,
                row_count=1,
                pagination_complete=True,
                quality_status=QualityStatus.PASS,
            )
            self.assertIs(readiness, Readiness.BLOCKED)
            self.assertIn(f"PROVENANCE_{provenance.value}", reasons)
