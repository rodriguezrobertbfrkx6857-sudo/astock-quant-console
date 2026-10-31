from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from qforge import config as legacy_config
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.contracts.safety import (
    ACCOUNT_ACCESS,
    BROKER_ACCESS,
    BROKER_AUTH,
    LOCAL_ONLY,
    REAL_ORDER_ENABLED,
    REAL_TRADING_ENABLED,
    PROJECT_ROOT,
    PathBoundary,
    SafetyViolation,
    validate_loopback_host,
    validate_runtime_safety,
)


class SafetyContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        legacy_config.reset_settings()

    def test_permanent_switches_have_exact_values(self) -> None:
        self.assertIs(REAL_TRADING_ENABLED, False)
        self.assertIs(REAL_ORDER_ENABLED, False)
        self.assertIs(BROKER_ACCESS, False)
        self.assertIs(ACCOUNT_ACCESS, False)
        self.assertIs(BROKER_AUTH, False)
        self.assertIs(LOCAL_ONLY, True)

    def test_all_enable_attempts_are_rejected(self) -> None:
        for switch in (
            "REAL_TRADING_ENABLED",
            "REAL_ORDER_ENABLED",
            "BROKER_ACCESS",
            "ACCOUNT_ACCESS",
            "BROKER_AUTH",
        ):
            for prefix in ("", "QF_", "V2_"):
                with self.subTest(variable=prefix + switch):
                    with patch.dict(os.environ, {prefix + switch: "true"}, clear=False):
                        with self.assertRaises(SafetyViolation):
                            validate_runtime_safety()

    def test_local_only_cannot_be_disabled(self) -> None:
        with patch.dict(os.environ, {"QF_LOCAL_ONLY": "false"}, clear=False):
            with self.assertRaises(SafetyViolation):
                validate_runtime_safety()

    def test_safety_is_enforced_under_python_optimized_mode(self) -> None:
        env = os.environ.copy()
        env["QF_REAL_ORDER_ENABLED"] = "true"
        completed = subprocess.run(
            [
                sys.executable,
                "-O",
                "-c",
                "from src.quantcore.contracts.safety import validate_runtime_safety; "
                "validate_runtime_safety()",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("permanently disabled", completed.stderr)

    def test_only_loopback_hosts_are_accepted(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost"):
            self.assertEqual(validate_loopback_host(host), host)
        for host in ("0.0.0.0", "192.168.1.8", "example.com", ""):
            with self.subTest(host=host), self.assertRaises(SafetyViolation):
                validate_loopback_host(host)

    def test_writes_are_confined_to_main_project(self) -> None:
        boundary = PathBoundary()
        self.assertEqual(
            boundary.require_writable(PROJECT_ROOT / "state" / "v2"),
            (PROJECT_ROOT / "state" / "v2").resolve(),
        )
        for forbidden in (PROJECT_ROOT.parent / "outside-market.db", PROJECT_ROOT.parent / "outside-data", PROJECT_ROOT.parent / "outside.db"):
            with self.subTest(path=forbidden), self.assertRaises(SafetyViolation):
                boundary.require_writable(forbidden)

    def test_legacy_qforge_env_cannot_redirect_writes(self) -> None:
        with patch.dict(os.environ, {"QF_DATA_DIR": str(PROJECT_ROOT.parent / "outside-data")}, clear=False):
            with self.assertRaises(SafetyViolation):
                legacy_config.load_settings()

    def test_write_capable_sqlite_connector_rejects_legacy_path(self) -> None:
        from qforge import db

        with self.assertRaises(SafetyViolation):
            db.connect(PROJECT_ROOT.parent / "forbidden-s00.db", "CREATE TABLE x(id INT)")

    def test_v2_config_loads_and_uses_main_project_paths(self) -> None:
        cfg = load_v2_config()
        boundary = PathBoundary()
        boundary.require_writable(cfg.paths.data)
        boundary.require_writable(cfg.paths.state)
        self.assertEqual(
            boundary.require_readable(cfg.paths.legacy_archive),
            cfg.paths.legacy_archive.resolve(),
        )
        self.assertEqual(cfg.schema_version, 2)

    def test_config_file_declares_all_six_invariants(self) -> None:
        raw = json.loads(
            (PROJECT_ROOT / "config" / "v2" / "settings.json").read_text("utf-8")
        )
        self.assertEqual(
            raw["safety"],
            {
                "REAL_TRADING_ENABLED": False,
                "REAL_ORDER_ENABLED": False,
                "BROKER_ACCESS": False,
                "ACCOUNT_ACCESS": False,
                "BROKER_AUTH": False,
                "LOCAL_ONLY": True,
            },
        )
