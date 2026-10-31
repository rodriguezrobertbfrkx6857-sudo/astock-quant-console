"""Bounded realtime-feed verification for the existing Watch provider.

The check is deliberately conservative: outside an actual A-share market
session it records PENDING_MARKET_SESSION and sends no vendor request.  During
a session it probes the four existing relay endpoints without persisting the
response body.  A single successful HTTP response is never promoted to READY.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.activation.realtime import (  # noqa: E402
    RealtimeStatus,
    TushareRelayRealtimeProvider,
)
from src.quantcore.activation.status import (  # noqa: E402
    current_business_date,
    publication_snapshot,
)
from src.quantcore.activation.vendor import RelayClient  # noqa: E402
from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.contracts.secrets import (  # noqa: E402
    ensure_relay_credential_from_approved_project_files,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ASSETS = ("510300.SH", "510500.SH", "159915.SZ")
ENDPOINTS = ("rt_k", "rt_min", "rt_min_daily", "stk_mins")
EVIDENCE_RELATIVE = Path("evidence") / "REAL_OPERATION" / "realtime_feed_check.json"
ACCEPTANCE_CONFIG = ROOT / "config" / "v2" / "watch_acceptance.json"


def _now() -> datetime:
    return datetime.now(UTC).astimezone(SHANGHAI)


def _session(now: datetime) -> str | None:
    current = now.time().replace(tzinfo=None)
    if time(9, 30) <= current <= time(11, 30):
        return "MORNING"
    if time(13, 0) <= current <= time(15, 0):
        return "AFTERNOON"
    return None


def _timestamp_value(row: dict[str, object]) -> str | None:
    for key in ("trade_time", "datetime", "timestamp", "time", "ts", "trade_date"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _parse_vendor_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _validate_rows(rows: tuple[dict[str, object], ...], api: str, *, now: datetime | None = None, freshness_seconds: int = 90) -> dict[str, object]:
    observations: list[tuple[str, str]] = []
    per_asset: dict[str, list[str]] = {}
    for index, row in enumerate(rows):
        timestamp = _timestamp_value(row)
        if not timestamp:
            continue
        asset = str(
            row.get("ts_code")
            or row.get("symbol")
            or row.get("code")
            or f"__row_without_asset__:{index}"
        )
        observations.append((asset, timestamp))
        per_asset.setdefault(asset, []).append(timestamp)
    timestamps = [timestamp for _, timestamp in observations]
    duplicate_count = len(observations) - len(set(observations))
    monotonic = all(values == sorted(values) for values in per_asset.values())
    timestamp_assets = sorted({asset for asset, _ in observations})
    cross_asset_shared = sum(
        1
        for timestamp in set(timestamps)
        if len({asset for asset, value in observations if value == timestamp}) > 1
    )
    timestamp_required = api != "rt_min_daily"
    complete_identity = len(observations) == len(rows) and bool(timestamp_assets)
    timestamp_pass = bool(timestamps) and complete_identity and monotonic and duplicate_count == 0
    newest = _parse_vendor_timestamp(max(timestamps) if timestamps else None)
    current = now or _now()
    age_seconds = (current - newest).total_seconds() if newest else None
    freshness_pass = bool(age_seconds is not None and -5 <= age_seconds <= freshness_seconds)
    return {
        "row_count": len(rows),
        "fields": sorted(set().union(*(row.keys() for row in rows))) if rows else [],
        "timestamp_count": len(timestamps),
        "source_timestamps": sorted(set(timestamps))[:500],
        "source_timestamp_min": min(timestamps) if timestamps else None,
        "source_timestamp_max": max(timestamps) if timestamps else None,
        "freshness_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "freshness_threshold_seconds": freshness_seconds,
        "freshness_validation": "PASS" if freshness_pass else "FAIL",
        "asset_count": len(timestamp_assets),
        "duplicate_asset_timestamps": duplicate_count,
        "cross_asset_shared_timestamps": cross_asset_shared,
        "per_asset_monotonic_timestamps": monotonic,
        "timestamp_validation": "PASS" if timestamp_pass or (not timestamp_required and timestamps) else "FAIL",
        "observation_fingerprint": hashlib.sha256(
            json.dumps(sorted(observations), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest() if observations else None,
        "semantics": "DAILY_AGGREGATE_DIAGNOSTIC_ONLY" if api == "rt_min_daily" else "INTRADAY_OBSERVATION",
    }


def _prior_success_samples(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    samples = payload.get("validated_samples")
    if not isinstance(samples, list):
        return []
    return [dict(item) for item in samples if isinstance(item, dict)]


def _phase(timestamp: str) -> str | None:
    try:
        current = datetime.fromisoformat(timestamp).astimezone(SHANGHAI).strftime("%H:%M")
    except (TypeError, ValueError):
        return None
    for window in ("09:30-09:45", "13:00-13:15", "14:45-15:00"):
        left, right = window.split("-")
        if left <= current <= right:
            return window
    return None


def _continuity(samples: list[dict[str, object]], required_minutes: int, maximum_missing_ratio: float) -> dict[str, object]:
    observed = []
    for sample in samples:
        try:
            observed.append(datetime.fromisoformat(str(sample["observed_at_asia_shanghai"])).astimezone(SHANGHAI))
        except (KeyError, TypeError, ValueError):
            continue
    observed = sorted(set(value.replace(second=0, microsecond=0) for value in observed))
    if len(observed) < 2:
        span = 0
    else:
        span = int((observed[-1] - observed[0]).total_seconds() // 60)
    expected = span + 1 if span else 1
    missing_ratio = max(0.0, 1.0 - len(observed) / expected)
    return {
        "observed_distinct_minutes": len(observed),
        "window_span_minutes": span,
        "expected_minutes": expected,
        "missing_ratio": round(missing_ratio, 6),
        "required_window_minutes": required_minutes,
        "maximum_missing_ratio": maximum_missing_ratio,
        "passed": span >= required_minutes and missing_ratio <= maximum_missing_ratio,
    }


def _status(cfg, status: str, detail: str, evidence: str) -> None:
    persisted_status = {
        "PENDING_MARKET_SESSION": "PENDING",
        "PENDING_DATA": "BLOCKED_DATA",
        "DEGRADED_VENDOR": "DEGRADED",
    }.get(status, status)
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) "
            "ON CONFLICT(component) DO UPDATE SET status=excluded.status,"
            "detail=excluded.detail,evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            ("intraday_live", persisted_status, detail, evidence, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def build() -> dict[str, Any]:
    cfg = load_v2_config(create_dirs=True)
    credential_source = ensure_relay_credential_from_approved_project_files()
    now = _now()
    business = current_business_date(cfg, now=now)
    business_text = business.isoformat() if business else None
    snapshot = publication_snapshot(cfg)
    run_id = f"realtime-{business_text or 'unresolved'}-{snapshot[:16]}"
    session = _session(now)
    path = cfg.paths.artifacts / EVIDENCE_RELATIVE
    validated_samples = _prior_success_samples(path)
    acceptance = json.loads(ACCEPTANCE_CONFIG.read_text("utf-8"))
    recovery_verified = False
    endpoint_results: list[dict[str, object]] = []
    request_performed = False
    business_compact = business.strftime("%Y%m%d") if business else None
    validated_samples = [
        item for item in validated_samples
        if business_compact
        and any(str(stamp).replace("-", "").startswith(business_compact) for stamp in item.get("source_timestamps", []))
    ]

    if business is None:
        status = "PENDING_DATA"
        reason = "trade calendar publication unavailable; realtime business date unresolved"
    elif session is None:
        status = "PENDING_MARKET_SESSION"
        reason = "outside 09:30-11:30 / 13:00-15:00 Asia/Shanghai; no vendor request performed"
    elif not RelayClient.configured():
        status = "PENDING_VENDOR"
        reason = "TUSHARE_RELAY_KEY absent in the worker environment"
    else:
        request_performed = True
        provider = TushareRelayRealtimeProvider(
            RelayClient(timeout_seconds=10.0, max_retries=0, min_interval_seconds=0.4)
        )
        params_by_api = {
            "rt_k": {},
            "rt_min": {"freq": "1min"},
            "rt_min_daily": {},
            "stk_mins": {"freq": "1min"},
        }
        for api in ENDPOINTS:
            result = provider.fetch_api(api, ASSETS, params=params_by_api[api])
            validation = _validate_rows(result.rows, api, now=now, freshness_seconds=int(acceptance["minute_bar_freshness_seconds"]))
            endpoint_results.append(
                {
                    "api": api,
                    "status": result.status.value,
                    "reason": result.reason,
                    "validation": validation,
                }
            )
        successful_intraday = [
            item
            for item in endpoint_results
            if item["status"] == RealtimeStatus.PASS.value
            and item["validation"]["timestamp_validation"] == "PASS"
            and item["validation"]["freshness_validation"] == "PASS"
            and item["validation"]["semantics"] == "INTRADAY_OBSERVATION"
            and int(item["validation"]["row_count"]) > 0
        ]
        if not successful_intraday:
            status = "DEGRADED_VENDOR"
            reason = "no requested intraday endpoint produced a timestamp-valid sample; bounded retry policy exhausted"
        else:
            sample = {
                "observed_at_asia_shanghai": now.isoformat(),
                "session": session,
                "endpoints": [str(item["api"]) for item in successful_intraday],
                "fingerprints": [str(item["validation"]["observation_fingerprint"]) for item in successful_intraday],
                "source_timestamps": sorted({stamp for item in successful_intraday for stamp in item["validation"].get("source_timestamps", [])}),
            }
            prior_fingerprints = {
                fingerprint
                for item in validated_samples
                for fingerprint in item.get("fingerprints", [])
                if isinstance(fingerprint, str)
            }
            if any(fingerprint not in prior_fingerprints for fingerprint in sample["fingerprints"]):
                validated_samples.append(sample)
            validated_samples = validated_samples[-500:]
        continuity = _continuity(validated_samples, int(acceptance["continuity_window_minutes"]), float(acceptance["maximum_missing_ratio"]))
        phases = sorted({phase for item in validated_samples if (phase := _phase(str(item.get("observed_at_asia_shanghai") or "")))})
        required_phases = list(acceptance["required_phase_windows"])
        if successful_intraday and len(validated_samples) < 3:
            status = "PENDING_VENDOR"
            reason = "a real intraday path passed, but three distinct timestamp samples are required"
        elif successful_intraday and (not continuity["passed"] or not set(required_phases).issubset(phases) or not recovery_verified):
            status = "INITIAL_CONNECTIVITY_PASS_CONTINUITY_PENDING"
            reason = "three samples passed, but the frozen continuity and all required market-phase windows are not yet verified"
        elif successful_intraday:
            status = "PASS"
            reason = "frozen initial, continuity, missing-ratio and market-phase acceptance gates passed"

    continuity = _continuity(validated_samples, int(acceptance["continuity_window_minutes"]), float(acceptance["maximum_missing_ratio"]))
    phases = sorted({phase for item in validated_samples if (phase := _phase(str(item.get("observed_at_asia_shanghai") or "")))})

    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "business_date": business_text,
        "scope": "LIVE_INTRADAY_WATCH",
        "snapshot": snapshot,
        "run_id": run_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "market_session": session is not None and business is not None,
        "session": session,
        "observed_at_asia_shanghai": now.isoformat(),
        "status": status,
        "reason": reason,
        "assets": list(ASSETS),
        "endpoints": endpoint_results,
        "request_performed": request_performed,
        "credential_path_status": (
            "VERIFIED_IN_MEMORY" if credential_source != "ABSENT" else "ABSENT"
        ),
        "validated_sample_count": len(validated_samples),
        "validated_samples": validated_samples,
        "initial_connectivity_pass": len(validated_samples) >= int(acceptance["initial_distinct_timestamps_per_asset"]),
        "continuity": continuity,
        "required_phase_windows": acceptance["required_phase_windows"],
        "verified_phase_windows": phases,
        "missing_phase_windows": sorted(set(acceptance["required_phase_windows"]).difference(phases)),
        "recovery_verified": recovery_verified,
        "full_acceptance": status == "PASS",
        "ready_rule": "PASS requires initial samples plus the frozen 30-minute continuity, missing-ratio, recovery and three phase-window gates; daily aggregates never qualify",
        "live_trading": False,
        "real_orders": False,
        "secrets_emitted": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    _status(
        cfg,
        status,
        f"business_date={business_text};scope=LIVE_INTRADAY_WATCH;snapshot={snapshot};run_id={run_id};{reason}",
        "artifacts/v2/evidence/REAL_OPERATION/realtime_feed_check.json",
    )
    return evidence


def main() -> int:
    evidence = build()
    print(json.dumps({
        "status": evidence["status"],
        "business_date": evidence["business_date"],
        "session": evidence["session"],
        "request_performed": evidence["request_performed"],
        "validated_sample_count": evidence["validated_sample_count"],
        "evidence": "artifacts/v2/evidence/REAL_OPERATION/realtime_feed_check.json",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
