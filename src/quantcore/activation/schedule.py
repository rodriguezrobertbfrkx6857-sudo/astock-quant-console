"""Local Shanghai exchange clock for safe Watch scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


class TradingPhase(StrEnum):
    OVERNIGHT = "OVERNIGHT"
    PREOPEN = "PREOPEN"
    LIVE_WATCH = "LIVE_WATCH"
    PAUSE = "PAUSE"
    POST_EOD = "POST_EOD"


@dataclass(frozen=True)
class ScheduleStatus:
    local_time: str
    business_date: str
    phase: TradingPhase
    watch_enabled: bool
    live_feed_status: str
    is_exchange_open_date: bool = False
    calendar_source: str = "PUBLISHED_TRADE_CAL_UNAVAILABLE"
    calendar_known: bool = False
    calendar_stale: bool = False
    calendar_latest_date: str | None = None
    calendar_manifest_hash: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "local_time": self.local_time,
            "business_date": self.business_date,
            "phase": self.phase.value,
            "watch_enabled": self.watch_enabled,
            "live_feed_status": self.live_feed_status,
            "calendar": {
                "is_exchange_open_date": self.is_exchange_open_date,
                "known": self.calendar_known,
                "stale": self.calendar_stale,
                "latest_date": self.calendar_latest_date,
                "manifest_hash": self.calendar_manifest_hash,
                "source": self.calendar_source,
            },
            "windows": {
                "preflight": "09:25-09:30",
                "morning_watch": "09:30-11:30",
                "midday_pause": "11:30-13:00",
                "afternoon_watch": "13:00-15:00",
                "eod": "15:00+",
            },
        }


def phase_at(moment: datetime) -> TradingPhase:
    local = moment.astimezone(SHANGHAI)
    current = local.time().replace(tzinfo=None)
    if current < time(9, 25):
        return TradingPhase.OVERNIGHT
    if current < time(9, 30):
        return TradingPhase.PREOPEN
    if current < time(11, 30):
        return TradingPhase.LIVE_WATCH
    if current < time(13, 0):
        return TradingPhase.PAUSE
    if current < time(15, 0):
        return TradingPhase.LIVE_WATCH
    return TradingPhase.POST_EOD


def status_at(
    moment: datetime | None = None,
    *,
    feed_status: str = "PENDING_VENDOR",
    trade_calendar: dict[date, bool] | None = None,
    calendar_manifest_hash: str | None = None,
) -> ScheduleStatus:
    current = (moment or datetime.now(UTC)).astimezone(SHANGHAI)
    phase = phase_at(current)
    calendar = trade_calendar or {}
    today = current.date()
    calendar_known = today in calendar
    is_open = bool(calendar.get(today, False)) if calendar_known else False
    latest = max(calendar) if calendar else None
    calendar_stale = latest is not None and latest < today
    source = (
        "PUBLISHED_TRADE_CAL"
        if calendar_known and calendar_manifest_hash
        else "PUBLISHED_TRADE_CAL_DATE_MISSING"
        if calendar_manifest_hash
        else "PUBLISHED_TRADE_CAL_UNAVAILABLE"
    )
    return ScheduleStatus(
        current.isoformat(),
        today.isoformat(),
        phase,
        is_open and phase in {TradingPhase.PREOPEN, TradingPhase.LIVE_WATCH},
        feed_status,
        is_open,
        source,
        calendar_known,
        calendar_stale,
        latest.isoformat() if latest else None,
        calendar_manifest_hash,
    )


__all__ = ["ScheduleStatus", "TradingPhase", "phase_at", "status_at"]
