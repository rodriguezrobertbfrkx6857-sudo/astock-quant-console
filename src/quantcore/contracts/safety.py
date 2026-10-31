"""Fail-closed process and filesystem safety boundaries for Integrated V2."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


REAL_TRADING_ENABLED = False
REAL_ORDER_ENABLED = False
BROKER_ACCESS = False
ACCOUNT_ACCESS = False
BROKER_AUTH = False
LOCAL_ONLY = True

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEGACY_PROJECT_ROOTS: tuple[Path, ...] = ()


class SafetyViolation(RuntimeError):
    """Raised before startup or I/O when a permanent boundary is violated."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _norm(path: str | Path) -> str:
    return os.path.normcase(str(_resolved(path)))


def _is_within(path: str | Path, root: str | Path) -> bool:
    candidate, boundary = _norm(path), _norm(root)
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


_DISABLED_SWITCHES = (
    "REAL_TRADING_ENABLED",
    "REAL_ORDER_ENABLED",
    "BROKER_ACCESS",
    "ACCOUNT_ACCESS",
    "BROKER_AUTH",
)


def validate_safety_overrides(values: Mapping[str, object]) -> None:
    """Reject any attempt to enable a permanent-off switch.

    The lookup accepts direct, QF_, and V2_ spellings so an old launcher cannot
    silently bypass the V2 contract.
    """
    upper = {str(k).upper(): v for k, v in values.items()}
    for name in _DISABLED_SWITCHES:
        for key in (name, f"QF_{name}", f"V2_{name}"):
            if key in upper and _truthy(upper[key]):
                raise SafetyViolation(f"{name} is permanently disabled")
    for key in ("LOCAL_ONLY", "QF_LOCAL_ONLY", "V2_LOCAL_ONLY"):
        if key in upper and not _truthy(upper[key]):
            raise SafetyViolation("LOCAL_ONLY is permanently enabled")


def validate_loopback_host(host: str) -> str:
    value = str(host).strip()
    if value.lower() == "localhost":
        return value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise SafetyViolation(f"non-loopback bind is forbidden: {value!r}")


@dataclass(frozen=True)
class PathBoundary:
    """Central authority for writable and legacy-read-only path decisions."""

    project_root: Path = PROJECT_ROOT
    legacy_roots: tuple[Path, ...] = LEGACY_PROJECT_ROOTS

    def require_writable(self, path: str | Path) -> Path:
        target = _resolved(path)
        if any(_is_within(target, root) for root in self.legacy_roots):
            raise SafetyViolation(f"legacy project is read-only: {target}")
        if not _is_within(target, self.project_root):
            raise SafetyViolation(f"write target is outside the main project: {target}")
        return target

    def require_readable(self, path: str | Path) -> Path:
        target = _resolved(path)
        allowed = (self.project_root, *self.legacy_roots)
        if not any(_is_within(target, root) for root in allowed):
            raise SafetyViolation(f"read target is outside approved roots: {target}")
        return target

    def require_legacy_readonly(self, path: str | Path) -> Path:
        target = _resolved(path)
        if not any(_is_within(target, root) for root in self.legacy_roots):
            raise SafetyViolation(f"not an approved legacy read-only path: {target}")
        return target

    def ensure_directories(self, paths: Iterable[str | Path]) -> tuple[Path, ...]:
        checked = tuple(self.require_writable(path) for path in paths)
        for path in checked:
            path.mkdir(parents=True, exist_ok=True)
        return checked


def validate_runtime_safety(
    host: str | None = None,
    overrides: Mapping[str, object] | None = None,
) -> None:
    invariants = {
        "REAL_TRADING_ENABLED": REAL_TRADING_ENABLED,
        "REAL_ORDER_ENABLED": REAL_ORDER_ENABLED,
        "BROKER_ACCESS": BROKER_ACCESS,
        "ACCOUNT_ACCESS": ACCOUNT_ACCESS,
        "BROKER_AUTH": BROKER_AUTH,
        "LOCAL_ONLY": LOCAL_ONLY,
    }
    expected = {
        "REAL_TRADING_ENABLED": False,
        "REAL_ORDER_ENABLED": False,
        "BROKER_ACCESS": False,
        "ACCOUNT_ACCESS": False,
        "BROKER_AUTH": False,
        "LOCAL_ONLY": True,
    }
    if invariants != expected:
        raise SafetyViolation("compiled safety invariants were modified")
    validate_safety_overrides(os.environ)
    if overrides:
        validate_safety_overrides(overrides)
    if host is not None:
        validate_loopback_host(host)
