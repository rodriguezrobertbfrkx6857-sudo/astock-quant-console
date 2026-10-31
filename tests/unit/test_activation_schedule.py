from __future__ import annotations

from datetime import UTC, date, datetime

from src.quantcore.activation.schedule import TradingPhase, status_at


def _moment(day: int, hour: int, minute: int = 0) -> datetime:
    # September 2026 Shanghai is UTC+8.
    return datetime(2026, 9, day, hour - 8, minute, tzinfo=UTC)


def test_published_open_session_controls_watch_windows() -> None:
    calendar = {date(2026, 9, 10): True}
    manifest = "a" * 64
    preopen = status_at(_moment(10, 9, 26), trade_calendar=calendar, calendar_manifest_hash=manifest)
    morning = status_at(_moment(10, 10), trade_calendar=calendar, calendar_manifest_hash=manifest)
    lunch = status_at(_moment(10, 12), trade_calendar=calendar, calendar_manifest_hash=manifest)
    afternoon = status_at(_moment(10, 14), trade_calendar=calendar, calendar_manifest_hash=manifest)
    post = status_at(_moment(10, 15, 1), trade_calendar=calendar, calendar_manifest_hash=manifest)

    assert preopen.phase is TradingPhase.PREOPEN and preopen.watch_enabled
    assert morning.phase is TradingPhase.LIVE_WATCH and morning.watch_enabled
    assert lunch.phase is TradingPhase.PAUSE and not lunch.watch_enabled
    assert afternoon.phase is TradingPhase.LIVE_WATCH and afternoon.watch_enabled
    assert post.phase is TradingPhase.POST_EOD and not post.watch_enabled
    assert morning.calendar_source == "PUBLISHED_TRADE_CAL"
    assert morning.calendar_known and not morning.calendar_stale


def test_published_holiday_and_weekend_never_use_weekday_fallback() -> None:
    calendar = {
        date(2026, 9, 12): False,  # weekend
        date(2026, 10, 1): False,  # published holiday
    }
    for moment in (_moment(12, 10), datetime(2026, 10, 1, 2, tzinfo=UTC)):
        status = status_at(moment, trade_calendar=calendar, calendar_manifest_hash="b" * 64)
        assert status.calendar_known
        assert not status.is_exchange_open_date
        assert not status.watch_enabled


def test_missing_or_stale_calendar_fails_closed() -> None:
    missing = status_at(_moment(10, 10))
    stale = status_at(
        _moment(10, 10),
        trade_calendar={date(2026, 9, 9): True},
        calendar_manifest_hash="c" * 64,
    )

    assert not missing.calendar_known and not missing.watch_enabled
    assert missing.calendar_source == "PUBLISHED_TRADE_CAL_UNAVAILABLE"
    assert not stale.calendar_known and stale.calendar_stale
    assert stale.calendar_source == "PUBLISHED_TRADE_CAL_DATE_MISSING"
    assert not stale.watch_enabled
