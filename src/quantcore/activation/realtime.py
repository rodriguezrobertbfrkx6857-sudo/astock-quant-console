"""Pluggable realtime boundary with a safe, non-networking default.

The quant core consumes this interface rather than knowing about a vendor.
Creating a provider never performs a network request; callers must explicitly
invoke ``fetch`` and must persist the returned raw observation before using it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Sequence

from src.quantcore.activation.vendor import RelayClient, VendorFailure


class RealtimeStatus(StrEnum):
    PASS = "PASS"
    PENDING_VENDOR = "PENDING_VENDOR"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class RealtimeResult:
    status: RealtimeStatus
    rows: tuple[dict[str, object], ...]
    reason: str


class RealtimeProvider(Protocol):
    """Vendor-neutral contract for explicitly requested realtime reads."""

    def fetch(self, asset_ids: Sequence[str]) -> RealtimeResult:
        ...


class DisabledRealtimeProvider:
    """Default provider; it makes the absence of a live feed explicit."""

    def fetch(self, asset_ids: Sequence[str]) -> RealtimeResult:
        del asset_ids
        return RealtimeResult(
            RealtimeStatus.DISABLED,
            (),
            "REALTIME_DATA is disabled until a vendor feed is explicitly verified",
        )


class TushareRelayRealtimeProvider:
    """Optional Tushare relay adapter.

    The relay transport remains isolated here.  This adapter does not promote
    a vendor response to canonical data; the caller must capture and quality
    check it through the normal REAL raw-object path first.
    """

    def __init__(self, client: RelayClient | None = None):
        self.client = client or RelayClient()

    def fetch_api(
        self,
        api: str,
        asset_ids: Sequence[str],
        *,
        params: dict[str, object] | None = None,
    ) -> RealtimeResult:
        """Fetch one explicitly named relay realtime endpoint.

        ``rt_min_daily`` is intentionally exposed for diagnostics only.  A
        caller must still validate timestamps, ordering, latency and repeated
        market-session samples before changing the persisted feed state.
        """

        allowed = {"rt_k", "rt_min", "rt_min_daily", "stk_mins"}
        if api not in allowed:
            raise ValueError(f"unsupported realtime endpoint: {api}")
        if not RelayClient.configured():
            return RealtimeResult(
                RealtimeStatus.PENDING_VENDOR,
                (),
                "PENDING_VENDOR:TUSHARE_RELAY_KEY absent",
            )
        if not asset_ids:
            return RealtimeResult(RealtimeStatus.DEGRADED, (), "no assets requested")
        request = dict(params or {})
        request.setdefault("ts_code", ",".join(sorted(set(asset_ids))))
        try:
            rows = self.client.fetch_page(api, request)
        except VendorFailure as exc:
            return RealtimeResult(RealtimeStatus.DEGRADED, (), f"vendor:{exc.code}")
        return RealtimeResult(
            RealtimeStatus.PASS,
            tuple(rows),
            f"{api} response received; repeated session validation and canonical promotion remain pending",
        )

    def fetch(self, asset_ids: Sequence[str]) -> RealtimeResult:
        return self.fetch_api("rt_k", asset_ids)
