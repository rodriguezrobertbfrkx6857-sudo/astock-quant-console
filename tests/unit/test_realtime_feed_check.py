from __future__ import annotations

import json

from scripts.v2.realtime_feed_check import _continuity, _prior_success_samples, _validate_rows


def test_cross_asset_shared_timestamp_is_valid() -> None:
    rows = (
        {"ts_code": "510300.SH", "trade_time": "2026-09-10 14:50:00", "price": 4.6},
        {"ts_code": "510500.SH", "trade_time": "2026-09-10 14:50:00", "price": 7.8},
        {"ts_code": "159915.SZ", "trade_time": "2026-09-10 14:50:00", "price": 2.1},
    )

    result = _validate_rows(rows, "rt_min")

    assert result["timestamp_validation"] == "PASS"
    assert result["duplicate_asset_timestamps"] == 0
    assert result["cross_asset_shared_timestamps"] == 1
    assert result["asset_count"] == 3


def test_same_asset_duplicate_timestamp_is_rejected() -> None:
    rows = (
        {"ts_code": "510300.SH", "trade_time": "2026-09-10 14:50:00"},
        {"ts_code": "510300.SH", "trade_time": "2026-09-10 14:50:00"},
    )

    result = _validate_rows(rows, "rt_min")

    assert result["timestamp_validation"] == "FAIL"
    assert result["duplicate_asset_timestamps"] == 1


def test_validated_samples_survive_outside_session_snapshot(tmp_path) -> None:
    target = tmp_path / "realtime.json"
    target.write_text(
        json.dumps(
            {
                "market_session": False,
                "status": "PENDING_MARKET_SESSION",
                "validated_samples": [{"session": "AFTERNOON", "fingerprints": ["abc"]}],
            }
        ),
        encoding="utf-8",
    )

    assert _prior_success_samples(target) == [
        {"session": "AFTERNOON", "fingerprints": ["abc"]}
    ]


def test_three_samples_do_not_satisfy_thirty_minute_continuity() -> None:
    samples = [
        {"observed_at_asia_shanghai": "2026-09-11T13:00:00+08:00"},
        {"observed_at_asia_shanghai": "2026-09-11T13:01:00+08:00"},
        {"observed_at_asia_shanghai": "2026-09-11T13:02:00+08:00"},
    ]
    result = _continuity(samples, 30, 0.1)
    assert result["passed"] is False
    assert result["window_span_minutes"] == 2


def test_sparse_window_fails_missing_ratio() -> None:
    samples = [
        {"observed_at_asia_shanghai": "2026-09-11T13:00:00+08:00"},
        {"observed_at_asia_shanghai": "2026-09-11T13:30:00+08:00"},
    ]
    result = _continuity(samples, 30, 0.1)
    assert result["window_span_minutes"] == 30
    assert result["missing_ratio"] > 0.9
    assert result["passed"] is False
