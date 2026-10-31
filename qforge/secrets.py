# -*- coding: utf-8 -*-
"""Credential loading -- env vars first, then gitignored local config.

Rules enforced here:
- Keys never appear in logs; `redact()` scrubs anything key-shaped.
- `describe()` returns only presence/length metadata, never values.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import ROOT

_SECRET_FILE = ROOT / "config" / "secrets.local.json"

_KEY_SHAPED = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|x-api-key)[\"']?\s*[:=]\s*[\"']?[^,;\s\"']{6,}",
    re.IGNORECASE,
)


def _load_file() -> dict:
    if not _SECRET_FILE.exists():
        return {}
    try:
        return json.loads(_SECRET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_api_key(source: str) -> str:
    """Return the API key for a vendor source ('datahubco' | 'promax') or ''.

    Resolution order: env (TUSHARE_DATAHUB_KEY / TUSHARE_PROMAX_KEY / generic
    QF_API_KEY_<SOURCE>) -> config/secrets.local.json -> ''.
    """
    env_names = {
        "datahubco": ["TUSHARE_DATAHUB_KEY", "DATAHUB_API_KEY"],
        "promax": ["TUSHARE_PROMAX_KEY", "TUSHARE_RELAY_KEY", "PROMAX_API_KEY"],
    }
    for name in env_names.get(source, []) + [f"QF_API_KEY_{source.upper()}"]:
        v = os.environ.get(name)
        if v:
            return v.strip()
    data = _load_file()
    key = ((data.get("sources") or {}).get(source) or {}).get("api_key")
    return (key or "").strip()


def has_source(source: str) -> bool:
    return bool(get_api_key(source))


def redact(text: str) -> str:
    """Scrub key-shaped substrings from a log line."""
    if not text:
        return text
    out = _KEY_SHAPED.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + ": ***REDACTED***", text)
    for src in ("datahubco", "promax"):
        k = get_api_key(src)
        if k and k in out:
            out = out.replace(k, "***REDACTED***")
    return out


def describe() -> dict:
    """Safe metadata for the dashboard -- presence only, never values."""
    out = {}
    for src in ("datahubco", "promax"):
        k = get_api_key(src)
        out[src] = {"configured": bool(k), "length": len(k) if k else 0, "source": "env" if bool(os.environ.get("TUSHARE_DATAHUB_KEY" if src == "datahubco" else "TUSHARE_PROMAX_KEY")) else ("local_config" if k else "missing")}
    return out
