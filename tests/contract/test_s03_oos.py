from __future__ import annotations
import hashlib
import inspect
import shutil
import sqlite3
import unittest
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.research.oos import (
    AccessDecision,
    AccessPurpose,
    OOSAccessService,
    ResultAccessGateway,
    import_exposure_file,
)


class S03OOSTests(unittest.TestCase):
    def setUp(self):
        cfg = load_v2_config()
        self.root = cfg.paths.state / "test_tmp" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.db = self.root / "control.sqlite"
        shutil.copy2(cfg.paths.state / "control.sqlite", self.db)
        self.service = OOSAccessService(self.db)
        self.gateway = ResultAccessGateway(self.service)

    def tearDown(self):
        shutil.rmtree(self.root)

    def request(self, start, end, purpose=AccessPurpose.RESEARCH, **extra):
        ph = hashlib.sha256(
            extra.get("protocol_hash", "protocol-renamed-v99").encode()
        ).hexdigest()
        sh = hashlib.sha256(
            extra.get("snapshot_hash", "different-data-hash").encode()
        ).hexdigest()
        return self.gateway.access(
            purpose,
            protocol_hash=ph,
            snapshot_hash=sh,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            asset_scope=extra.get("asset_scope", "GLOBAL"),
            family_scope=extra.get("family_scope", "GLOBAL"),
        )

    def test_known_window_cannot_be_reset_by_names_versions_or_hashes(self):
        for protocol, snapshot in (
            ("renamed", "newhash"),
            ("other-project-v10", "otherdata"),
        ):
            self.assertIs(
                self.request(
                    "2020-01-01",
                    "2020-01-31",
                    protocol_hash=protocol,
                    snapshot_hash=snapshot,
                ).decision,
                AccessDecision.ALLOW_KNOWN,
            )

    def test_partial_intersection_and_unknown_are_denied(self):
        self.assertIs(
            self.request("2025-08-01", "2025-09-01").decision,
            AccessDecision.DENY_UNKNOWN,
        )
        self.assertIs(
            self.request("2027-01-01", "2027-01-10").decision,
            AccessDecision.DENY_UNKNOWN,
        )

    def test_all_entrypoints_use_same_authority(self):
        for purpose in AccessPurpose:
            self.assertIs(
                self.request("2020-02-01", "2020-02-03", purpose).decision,
                AccessDecision.ALLOW_KNOWN,
            )

    def test_no_force_or_consume_boolean_exists(self):
        names = inspect.signature(OOSAccessService.authorize_known).parameters
        self.assertNotIn("force", names)
        self.assertNotIn("consume_oos", names)
        self.assertNotIn("uses_oos", names)

    def test_reserved_range_denies_even_if_known(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO window_reservations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "reserve-1",
                "p",
                "s",
                "2020-01-01",
                "2020-12-31",
                "GLOBAL",
                "GLOBAL",
                "RESERVED",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        self.assertIs(
            self.request("2020-03-01", "2020-03-02").decision,
            AccessDecision.DENY_RESERVED,
        )

    def test_synthetic_exposure_does_not_authorize_real_access(self):
        conn = sqlite3.connect(self.db)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO exposure_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-only",
                "synthetic-key",
                now,
                now,
                "fixture",
                "fixture",
                "a" * 64,
                None,
                None,
                "2030-01-01",
                "2030-12-31",
                "CN_TRADING",
                "GLOBAL",
                "GLOBAL",
                "SYNTHETIC",
                "SYNTHETIC_EXPOSURE",
                "VERIFIED",
                "TEAM",
                None,
                None,
            ),
        )
        conn.commit()
        conn.close()
        self.assertIs(
            self.request("2030-02-01", "2030-02-02").decision,
            AccessDecision.DENY_UNKNOWN,
        )

    def test_import_is_idempotent_and_has_no_2099(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "v2"
            / "oos_known_lower_bound.json"
        )
        result = import_exposure_file(self.db, source)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["duplicates"], 8)
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM exposure_events WHERE end_date>='2099-01-01'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_exposure_and_receipts_are_append_only(self):
        receipt = self.request("2020-04-01", "2020-04-02")
        conn = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM exposure_events")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE access_receipts SET decision='DENY_UNKNOWN' WHERE receipt_id=?",
                (receipt.receipt_id,),
            )
        conn.close()
