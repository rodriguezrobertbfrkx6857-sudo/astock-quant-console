"""The explicitly bounded real-data operational profile.

The profile is a user-facing capability boundary.  It is validated against
the current immutable data publications before a demo or supported daily run
can claim PASS.  A profile never widens a dataset's coverage and never turns
missing full-market data into a synthetic substitute.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.quantcore.contracts.config import V2Config, load_v2_config
from src.quantcore.contracts.safety import PathBoundary


@dataclass(frozen=True)
class ProfileValidation:
    profile_id: str
    status: str
    required_manifests: dict[str, dict[str, Any]]
    supported_securities: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "ready": self.ready,
            "required_manifests": self.required_manifests,
            "supported_securities": list(self.supported_securities),
            "reasons": list(self.reasons),
        }


def profile_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else Path.cwd()
    return PathBoundary().require_readable(root / "config" / "v2" / "operational_profile.json")


def load_operational_profile(cfg: V2Config | None = None) -> dict[str, Any]:
    config = cfg or load_v2_config()
    path = profile_path(config.source_path.parents[2])
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("profile_id") not in {
        "BOUNDED_REAL_OPERATIONAL_PROFILE_V1", "PUBLIC_FRAMEWORK_EXAMPLE"
    }:
        raise ValueError("unsupported operational profile")
    return payload


def validate_operational_profile(
    cfg: V2Config | None = None,
) -> ProfileValidation:
    config = cfg or load_v2_config()
    profile = load_operational_profile(config)
    required = profile.get("required_manifests", {})
    if not isinstance(required, dict):
        raise ValueError("operational profile required_manifests must be an object")
    connection = sqlite3.connect(
        f"file:{(config.paths.state / 'control.sqlite').as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    actual: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    try:
        for dataset, expected_value in required.items():
            expected = expected_value if isinstance(expected_value, dict) else {}
            row = connection.execute(
                "SELECT p.manifest_id,p.manifest_hash,m.manifest_json,m.readiness,m.provenance "
                "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
                "WHERE p.dataset=?",
                (dataset,),
            ).fetchone()
            if row is None:
                reasons.append(f"MISSING_PUBLICATION:{dataset}")
                continue
            manifest = json.loads(str(row["manifest_json"]))
            record = {
                "manifest_id": row["manifest_id"],
                "manifest_hash": row["manifest_hash"],
                "readiness": row["readiness"],
                "provenance": row["provenance"],
                "row_count": manifest.get("row_count"),
                "start_date": manifest.get("start_date"),
                "end_date": manifest.get("end_date"),
                "symbol_count": manifest.get("symbol_count"),
                "available_at_policy": manifest.get("available_at_policy"),
                "purpose": expected.get("purpose"),
            }
            actual[dataset] = record
            if row["readiness"] != "REAL_READY" or row["provenance"] != "REAL":
                reasons.append(f"PUBLICATION_NOT_REAL_READY:{dataset}")
            if str(row["manifest_hash"]) != str(expected.get("manifest_hash")):
                reasons.append(f"MANIFEST_HASH_MISMATCH:{dataset}")
    finally:
        connection.close()
    securities = tuple(
        str(item.get("ts_code"))
        for item in profile.get("supported_securities", [])
        if isinstance(item, dict) and item.get("ts_code")
    )
    if len(securities) != len(set(securities)) or not securities:
        reasons.append("PROFILE_SECURITIES_INVALID")
    return ProfileValidation(
        str(profile["profile_id"]),
        "PASS" if not reasons else "FAIL",
        actual,
        securities,
        tuple(reasons),
    )


__all__ = [
    "ProfileValidation",
    "load_operational_profile",
    "profile_path",
    "validate_operational_profile",
]
