"""Versioned, dependency-free loader for config/v2/settings.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import PROJECT_ROOT, PathBoundary, SafetyViolation, validate_runtime_safety


@dataclass(frozen=True)
class V2Paths:
    data: Path
    state: Path
    artifacts: Path
    logs: Path
    legacy_archive: Path


@dataclass(frozen=True)
class V2Server:
    host: str
    port: int


@dataclass(frozen=True)
class V2Resources:
    minimum_free_bytes: int
    migration_headroom_bytes: int


@dataclass(frozen=True)
class V2Config:
    schema_version: int
    paths: V2Paths
    server: V2Server
    resources: V2Resources
    source_path: Path


def _object(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise SafetyViolation(f"config section {key!r} must be an object")
    return value


def _inside(boundary: PathBoundary, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SafetyViolation(f"config path {label!r} must be non-empty")
    path = Path(value)
    if not path.is_absolute():
        path = boundary.project_root / path
    return boundary.require_writable(path)


def load_v2_config(
    path: str | Path | None = None, *, create_dirs: bool = False
) -> V2Config:
    source = Path(path) if path else PROJECT_ROOT / "config" / "v2" / "settings.json"
    source = source.resolve(strict=True)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 2:
        raise SafetyViolation("Integrated V2 requires schema_version=2")
    safety = _object(raw, "safety")
    validate_runtime_safety(overrides=safety)

    boundary = PathBoundary()
    paths = _object(raw, "paths")
    v2_paths = V2Paths(
        data=_inside(boundary, paths.get("data"), "data"),
        state=_inside(boundary, paths.get("state"), "state"),
        artifacts=_inside(boundary, paths.get("artifacts"), "artifacts"),
        logs=_inside(boundary, paths.get("logs"), "logs"),
        legacy_archive=_inside(boundary, paths.get("legacy_archive"), "legacy_archive"),
    )
    server = _object(raw, "server")
    host = str(server.get("host", ""))
    validate_runtime_safety(host=host)
    port = server.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SafetyViolation("server.port must be an integer in 1..65535")

    resources = _object(raw, "resources")
    minimum_free = resources.get("minimum_free_bytes")
    headroom = resources.get("migration_headroom_bytes")
    if not isinstance(minimum_free, int) or minimum_free < 0:
        raise SafetyViolation(
            "resources.minimum_free_bytes must be a non-negative integer"
        )
    if not isinstance(headroom, int) or headroom < 0:
        raise SafetyViolation(
            "resources.migration_headroom_bytes must be a non-negative integer"
        )

    if create_dirs:
        boundary.ensure_directories(
            (v2_paths.data, v2_paths.state, v2_paths.artifacts, v2_paths.logs)
        )
    return V2Config(
        schema_version=2,
        paths=v2_paths,
        server=V2Server(host=host, port=port),
        resources=V2Resources(minimum_free, headroom),
        source_path=source,
    )
