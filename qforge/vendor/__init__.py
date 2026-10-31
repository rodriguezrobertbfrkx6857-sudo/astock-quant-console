# -*- coding: utf-8 -*-
"""Tushare Relay client for the two bundled gateway sources.

Both sources are GET endpoints authenticated by the ``X-API-Key`` header:
  datahubco : https://data.example.invalid/tushare/{api}
  promax    : https://relay.example.invalid/tushare/pro/{api}

Behaviours implemented per vendor docs:
- business params always present (bare requests are treated as probes upstream)
- 429 -> honour Retry-After; 502/503/504 & network errors -> bounded exp backoff
- per-(source, api) circuit breaker with cooldown (vendor failure isolation)
- client-side pacing far below the 200 req/min per-IP cap
- keys are injected from env/local secrets only; never logged (redact filter)
- responses parsed from data.fields + data.items (Tushare compatible)
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
import requests

from .. import secrets as sec
from ..config import get_settings
from ..util import get_logger, json_dumps

log = get_logger("qforge.vendor")

RETRYABLE_HTTP = {429, 502, 503, 504}


class VendorError(RuntimeError):
    def __init__(self, source: str, api: str, message: str, http: int | None = None,
                 retryable: bool = False, retry_after: float | None = None):
        super().__init__(f"[{source}/{api}] {message}")
        self.source, self.api, self.http = source, api, http
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        s = get_settings()
        if self.failures < s.breaker_fail_threshold:
            return False
        if time.monotonic() - self.opened_at >= s.breaker_cooldown_sec:
            self.failures = 0  # half-open probe
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        self.opened_at = time.monotonic()


class RelayClient:
    """One client per vendor source. Thread-safe pacing via a lock."""

    def __init__(self, source: str, base_url: str, transport: Callable | None = None):
        self.source = source
        self.base_url = base_url.rstrip("/")
        self._transport = transport or self._requests_transport
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._breakers: dict[str, _Breaker] = {}
        self._session = requests.Session()
        self._session.trust_env = True  # honour NO_PROXY etc.
        self.stats = {"calls": 0, "retries": 0, "errors": 0, "breaker_skips": 0, "cache_hits": 0}
        self._cache: dict[str, tuple[float, Any]] = {}  # tiny short-TTL dedupe cache

    # ------------------------------------------------------------------ low level
    @staticmethod
    def _requests_transport(base_url: str, api: str, params: dict, headers: dict,
                            timeout: float, verify: bool):
        url = f"{base_url}/{api}"
        r = requests.get(url, params=params, headers=headers, timeout=timeout, verify=verify)
        return r.status_code, dict(r.headers), r

    def _throttle(self) -> None:
        s = get_settings()
        min_iv = s.datahubco_min_interval if self.source == "datahubco" else s.promax_min_interval
        with self._lock:
            wait = self._last_call + min_iv - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 5.0))
            self._last_call = time.monotonic()

    def breaker(self, api: str) -> _Breaker:
        if api not in self._breakers:
            self._breakers[api] = _Breaker()
        return self._breakers[api]

    # ------------------------------------------------------------------ public
    def fetch_raw(self, api: str, params: dict | None = None) -> dict:
        """Return the parsed vendor JSON body for one request (with retries)."""
        s = get_settings()
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        brk = self.breaker(api)
        if brk.is_open:
            self.stats["breaker_skips"] += 1
            raise VendorError(self.source, api, "circuit breaker open (recent failures)",
                              retryable=True, retry_after=s.breaker_cooldown_sec)

        headers = {"X-API-Key": sec.get_api_key(self.source),
                   "X-Request-Id": uuid.uuid4().hex[:16]}
        cache_key = f"{api}|{json_dumps(params, sort_keys=True) if False else ''}"  # placeholder
        last_err: Exception | None = None

        for attempt in range(s.vendor_max_retries + 1):
            self._throttle()
            self.stats["calls"] += 1
            try:
                status, rheaders, resp = self._transport(
                    self.base_url, api, params, headers, s.vendor_timeout, s.verify_ssl)
                retry_after = None
                try:
                    retry_after = float(rheaders.get("Retry-After") or 0) or None
                except (TypeError, ValueError):
                    retry_after = None
                if status in RETRYABLE_HTTP:
                    self.stats["retries"] += 1
                    raise VendorError(self.source, api, f"http {status}", http=status,
                                      retryable=True, retry_after=retry_after)
                try:
                    body = resp.json() if hasattr(resp, "json") else resp
                except ValueError:
                    raise VendorError(self.source, api, f"http {status}: non-JSON response",
                                      http=status, retryable=False)
                if status >= 400 or body.get("ok") is False:
                    err = body.get("error") or "http_error"
                    msg = body.get("message") or body.get("msg") or ""
                    raise VendorError(self.source, api, f"{err}: {msg}".strip(": "), http=status,
                                      retryable=status in RETRYABLE_HTTP,
                                      retry_after=retry_after)
                code = body.get("code", 0)
                if code not in (0, None):
                    # upstream business error: not retryable by default
                    raise VendorError(self.source, api, f"upstream code={code}: {body.get('msg','')}",
                                      retryable=False)
                brk.record_success()
                return body
            except VendorError as e:
                last_err = e
                if not e.retryable or attempt >= s.vendor_max_retries:
                    brk.record_failure()
                    self.stats["errors"] += 1
                    raise
                delay = e.retry_after if e.retry_after else (s.vendor_backoff_base ** attempt)
                delay = min(delay, 60.0)
                log.warning("vendor retry %s/%s %s/%s in %.1fs (%s)", attempt + 1,
                            s.vendor_max_retries, self.source, api, delay, e)
                time.sleep(delay)
            except requests.RequestException as e:  # network layer
                last_err = e
                self.stats["retries"] += 1
                if attempt >= s.vendor_max_retries:
                    brk.record_failure()
                    self.stats["errors"] += 1
                    raise VendorError(self.source, api, f"network error: {e}", retryable=True) from e
                delay = min(s.vendor_backoff_base ** attempt, 60.0)
                time.sleep(delay)
        raise VendorError(self.source, api, f"exhausted retries: {last_err}", retryable=True)

    # ------------------------------------------------------------------ parse
    @staticmethod
    def parse_rows(body: dict) -> list[dict]:
        data = body.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        rows = []
        for it in items:
            if len(fields) == len(it):
                rows.append(dict(zip(fields, it)))
            else:  # defensive: length mismatch -> positional best effort
                rows.append({f: it[i] if i < len(it) else None for i, f in enumerate(fields)})
        return rows

    def fetch(self, api: str, params: dict | None = None) -> list[dict]:
        body = self.fetch_raw(api, params)
        return self.parse_rows(body)

    def fetch_df(self, api: str, params: dict | None = None) -> pd.DataFrame:
        rows = self.fetch(api, params)
        return pd.DataFrame(rows)

    def fetch_paged(self, api: str, params: dict | None = None, page: int = 5000,
                    max_pages: int = 200) -> list[dict]:
        """offset/limit pagination; keeps business params fixed, increments offset."""
        params = dict(params or {})
        all_rows: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            p = {**params, "limit": page, "offset": offset}
            rows = self.fetch(api, p)
            all_rows.extend(rows)
            if len(rows) < page:
                break
            offset += page
        return all_rows

    def probe(self, api: str) -> dict:
        """Read-only capability probe (<=5 local rows, no upstream call)."""
        return self.fetch_raw(api, {"__probe": 1})


# ---------------------------------------------------------------- factories ----
_clients: dict[str, RelayClient] = {}
_client_lock = threading.Lock()


def get_client(source: str) -> RelayClient:
    s = get_settings()
    with _client_lock:
        if source in _clients:
            return _clients[source]
        if source == "datahubco":
            c = RelayClient("datahubco", s.datahubco_base)
        elif source == "promax":
            c = RelayClient("promax", s.promax_base)
        else:
            raise ValueError(f"unknown vendor source: {source}")
        _clients[source] = c
        return c


def available_sources() -> list[str]:
    s = get_settings()
    out = []
    if s.datahubco_enabled and sec.has_source("datahubco"):
        out.append("datahubco")
    if s.promax_enabled and sec.has_source("promax"):
        out.append("promax")
    return out


# per-dataset preferred source; None = try both in order
DATASET_SOURCE: dict[str, str | None] = {
    # verified local fast paths on datahubco
    "daily": "datahubco", "daily_basic": "datahubco", "adj_factor": "datahubco",
    "index_daily": "datahubco", "index_dailybasic": "datahubco", "fund_daily": "datahubco",
    "cb_daily": "datahubco", "stock_basic": "datahubco", "trade_cal": "datahubco",
    # realtime / news / special tables live on promax
    "anns_d": "promax", "news": "promax", "major_news": "promax", "express_news": "promax",
    "rt_k": "promax", "rt_min": "promax", "stk_mins": "promax",
    "suspend_d": "promax", "namechange": "promax", "stk_limit": "promax",
    "st": "promax", "stock_st": "promax", "index_weight": "promax",
    "fina_indicator": "promax", "dividend": "promax", "income": "promax",
    "balancesheet": "promax", "cashflow": "promax", "forecast": "promax",
    "express": "promax", "fund_basic": "promax", "new_share": "promax",
}


def fetch(api: str, params: dict | None = None) -> tuple[list[dict], str]:
    """Fetch a dataset trying preferred source first, then the other one.

    Returns (rows, source_used). Raises VendorError if all sources fail.
    """
    s = get_settings()
    sources = available_sources()
    if not sources:
        raise VendorError("none", api, "no vendor key configured (see config/secrets.local.json or env)")
    pref = DATASET_SOURCE.get(api)
    order = ([pref] if pref in sources else []) + [x for x in sources if x != pref]
    last: Exception | None = None
    for src in order:
        try:
            return get_client(src).fetch(api, params), src
        except VendorError as e:
            # 404 unknown_api / 503 data_source_unavailable -> try next source
            last = e
            log.warning("dataset %s failed on %s: %s", api, src, e)
    raise last  # type: ignore[misc]


def fetch_paged(api: str, params: dict | None = None) -> tuple[list[dict], str]:
    sources = available_sources()
    if not sources:
        raise VendorError("none", api, "no vendor key configured")
    pref = DATASET_SOURCE.get(api)
    order = ([pref] if pref in sources else []) + [x for x in sources if x != pref]
    last: Exception | None = None
    for src in order:
        try:
            return get_client(src).fetch_paged(api, params), src
        except VendorError as e:
            last = e
            log.warning("dataset %s (paged) failed on %s: %s", api, src, e)
    raise last  # type: ignore[misc]
