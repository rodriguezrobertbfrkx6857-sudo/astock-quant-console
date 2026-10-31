from __future__ import annotations

from datetime import UTC, datetime, timedelta

from quantweb.v2_api import _enrich_operation, _operation_scope, _status_semantics


def test_current_and_historical_operational_scopes_are_separate() -> None:
    assert _operation_scope("current_etf_scope") == "CURRENT_ETF3"
    assert _operation_scope("data:trade_cal") == "CURRENT_SHARED"
    assert _operation_scope("data:daily") == "FULL_MARKET_OPTIONAL"
    assert _operation_scope("startup_incremental_sync") == "FULL_MARKET_OPTIONAL"
    assert _operation_scope("historical_universe") == "HISTORICAL_AUDIT"
    assert _operation_scope("data_history:fund_daily") == "HISTORICAL_AUDIT"


def test_status_semantics_do_not_hide_failures_or_escalate_history() -> None:
    failure = _status_semantics(
        "FAIL", component="runtime_loop", scope="CURRENT_ETF3", detail="crashed"
    )
    history = _status_semantics(
        "BLOCKED_DATA",
        component="historical_universe",
        scope="HISTORICAL_AUDIT",
        detail="retained",
    )
    waiting = _status_semantics(
        "PENDING_VENDOR",
        component="intraday_live",
        scope="CURRENT_ETF3",
        detail="vendor unavailable",
    )

    assert failure["severity"] == "CRITICAL"
    assert history["severity"] == "INFO"
    assert waiting["severity"] == "WARN"
    assert waiting["reason"] == "EXPECTED_WAIT_OR_EXTERNAL_DEPENDENCY"


def test_operational_freshness_is_bound_to_checked_at() -> None:
    checked_at = datetime(2026, 9, 10, 12, tzinfo=UTC)
    fresh = _enrich_operation(
        {
            "component": "scheduler",
            "status": "PASS",
            "detail": "calendar verified",
            "updated_at": (checked_at - timedelta(seconds=30)).isoformat(),
        },
        checked_at=checked_at,
    )
    stale = _enrich_operation(
        {
            "component": "scheduler",
            "status": "PASS",
            "detail": "old snapshot",
            "updated_at": (checked_at - timedelta(hours=1)).isoformat(),
        },
        checked_at=checked_at,
    )

    assert fresh["freshness"] == "FRESH"
    assert stale["freshness"] == "STALE"
    assert fresh["checked_at"] == checked_at.isoformat()
