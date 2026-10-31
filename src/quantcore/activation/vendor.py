"""Restricted Tushare relay transport.

The credential is read by exact name from process memory and is never added to
URLs, persisted request metadata, exception messages, or logs.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import IncompleteRead
from dataclasses import dataclass
from typing import Callable


RELAY_BASE = "https://relay.example.invalid/tushare/pro"
RETRYABLE_HTTP = {429, 502, 503, 504}


@dataclass(frozen=True)
class VendorFailure(RuntimeError):
    code: str
    retryable: bool
    status: int | None = None

    def __str__(self) -> str:
        return self.code


class RelayClient:
    def __init__(
        self,
        *,
        base_url: str = RELAY_BASE,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        min_interval_seconds: float = 0.4,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if base_url != RELAY_BASE:
            raise ValueError("relay endpoint is pinned to the approved HTTPS host")
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleeper
        self._last_call = 0.0
        # Safe, non-secret evidence for the most recent paged read.  This is
        # intentionally kept in memory; callers may persist the page lengths
        # and offsets, never the credential or response body.
        self.last_pagination: dict[str, object] = {}
        self._last_response_meta: dict[str, object] = {}

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("TUSHARE_RELAY_KEY", "").strip())

    @staticmethod
    def _key() -> str:
        key = os.environ.get("TUSHARE_RELAY_KEY", "").strip()
        if not key:
            raise VendorFailure("VENDOR_AUTH_REQUIRED", False)
        return key

    def _pace(self) -> None:
        delay = self._last_call + self.min_interval_seconds - time.monotonic()
        if delay > 0:
            self._sleep(delay)
        self._last_call = time.monotonic()

    def fetch_page(
        self, api: str, params: dict[str, object]
    ) -> list[dict[str, object]]:
        if not api.replace("_", "").isalnum():
            raise ValueError("invalid relay API name")
        clean = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
        url = f"{self.base_url}/{api}?{urllib.parse.urlencode(clean)}"
        last: VendorFailure | None = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            request = urllib.request.Request(
                url,
                headers={"X-API-Key": self._key(), "Accept": "application/json"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                if not isinstance(body, dict) or body.get("code") not in (0, None):
                    raise VendorFailure("VENDOR_BUSINESS_ERROR", False)
                data = body.get("data") or {}
                fields, items = data.get("fields"), data.get("items")
                if not isinstance(fields, list) or not isinstance(items, list):
                    raise VendorFailure("SCHEMA_MISMATCH", False)
                rows: list[dict[str, object]] = []
                for item in items:
                    if not isinstance(item, list) or len(item) != len(fields):
                        raise VendorFailure("SCHEMA_MISMATCH", False)
                    rows.append(dict(zip(fields, item, strict=True)))
                self._last_response_meta = {
                    "total": data.get("total") if isinstance(data.get("total"), int) else None,
                    "has_more": data.get("has_more") if isinstance(data.get("has_more"), bool) else None,
                }
                return rows
            except urllib.error.HTTPError as exc:
                code = f"HTTP_{exc.code}"
                last = VendorFailure(code, exc.code in RETRYABLE_HTTP, exc.code)
            except (urllib.error.URLError, TimeoutError, socket.timeout):
                last = VendorFailure("NETWORK_TIMEOUT", True)
            except (IncompleteRead, ConnectionResetError, BrokenPipeError):
                # A relay can close a large response after sending only part
                # of the body.  Treat it as a bounded retryable transport
                # failure; never let a worker crash and leave its lease live.
                last = VendorFailure("NETWORK_INCOMPLETE_RESPONSE", True)
            except (UnicodeDecodeError, json.JSONDecodeError):
                last = VendorFailure("NON_JSON_RESPONSE", False)
            if last is not None and (not last.retryable or attempt >= self.max_retries):
                raise last
            self._sleep(min(2**attempt, 10))
        raise last or VendorFailure("VENDOR_UNKNOWN_FAILURE", False)

    def fetch_pages(
        self,
        api: str,
        params: dict[str, object],
        *,
        page_size: int = 5000,
        max_pages: int = 20,
    ) -> tuple[list[list[dict[str, object]]], bool]:
        pages: list[list[dict[str, object]]] = []
        self.last_pagination = {}
        offsets: list[int] = []
        response_totals: list[int] = []
        has_more_values: list[bool] = []
        if page_size <= 0 or max_pages <= 0:
            raise ValueError("page_size and max_pages must be positive")
        for page_number in range(max_pages):
            offset = page_number * page_size
            offsets.append(offset)
            page = self.fetch_page(
                api, {**params, "limit": page_size, "offset": offset}
            )
            pages.append(page)
            total = self._last_response_meta.get("total")
            if isinstance(total, int) and total >= 0:
                response_totals.append(total)
            has_more = self._last_response_meta.get("has_more")
            if isinstance(has_more, bool):
                has_more_values.append(has_more)
            if len(page) < page_size:
                total_hint = response_totals[-1] if response_totals else None
                self.last_pagination = {
                    "api": api,
                    "page_size": page_size,
                    "max_pages": max_pages,
                    "offsets": offsets,
                    "page_lengths": [len(item) for item in pages],
                    "pages_received": len(pages),
                    "complete": True,
                    "termination": "HAS_MORE_FALSE" if has_more_values and has_more_values[-1] is False else "SHORT_PAGE",
                    "total_hint": total_hint,
                    "exact_page_limit": bool(pages and len(pages[0]) == page_size),
                    # A short continuation response is the evidence that the
                    # server accepted the offset.  It is still called out when
                    # the first page sits exactly on the request limit so a
                    # report can distinguish an observed boundary from an
                    # assumption based on yesterday's row count.
                    "offset_termination_verified": len(pages) > 1,
                    "boundary_review": "REVIEW_EXACT_PAGE_LIMIT"
                    if pages and len(pages[0]) == page_size and total_hint is None
                    else "NOT_REQUIRED",
                }
                return pages, True
        self.last_pagination = {
            "api": api,
            "page_size": page_size,
            "max_pages": max_pages,
            "offsets": offsets,
            "page_lengths": [len(item) for item in pages],
            "pages_received": len(pages),
            "complete": False,
            "termination": "MAX_PAGES_REACHED",
            "total_hint": response_totals[-1] if response_totals else None,
            "exact_page_limit": bool(pages and len(pages[0]) == page_size),
            "offset_termination_verified": False,
            "boundary_review": "PAGINATION_MAX_PAGES_REACHED",
        }
        return pages, False
