from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum


class Confidence(StrEnum):
    VERIFIED = "VERIFIED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class SecurityType(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"
    ETF_DOMESTIC_EQUITY = "ETF_DOMESTIC_EQUITY"
    ETF_BOND = "ETF_BOND"
    ETF_GOLD_COMMODITY = "ETF_GOLD_COMMODITY"
    ETF_CROSS_BORDER = "ETF_CROSS_BORDER"
    ETF_MONEY = "ETF_MONEY"
    INDEX = "INDEX"
    UNKNOWN = "UNKNOWN"


class Board(StrEnum):
    SSE_MAIN = "SSE_MAIN"
    STAR = "STAR"
    SZSE_MAIN = "SZSE_MAIN"
    CHINEXT = "CHINEXT"
    BSE = "BSE"
    FUND = "FUND"
    UNKNOWN = "UNKNOWN"


class RiskStatus(StrEnum):
    NORMAL = "NORMAL"
    ST = "ST"
    DELISTING = "DELISTING"
    OTHER_RISK = "OTHER_RISK"
    UNKNOWN = "UNKNOWN"


class TradingStatus(StrEnum):
    TRADABLE = "TRADABLE"
    SUSPENDED = "SUSPENDED"
    NOT_LISTED = "NOT_LISTED"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


def require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SecurityIdentity:
    asset_id: str
    security_type: SecurityType
    exchange: str
    source_ref: str

    def __post_init__(self) -> None:
        if not all((self.asset_id, self.exchange, self.source_ref)):
            raise ValueError("security identity fields are required")


@dataclass(frozen=True)
class IdentifierVersion:
    asset_id: str
    scheme: str
    value: str
    effective_from: date
    effective_to: date | None
    known_from: datetime
    known_to: datetime | None
    source_ref: str
    confidence: Confidence

    def __post_init__(self) -> None:
        require_aware(self.known_from, "known_from")
        if self.known_to:
            require_aware(self.known_to, "known_to")
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective interval must be half-open and positive")
        if self.known_to and self.known_to <= self.known_from:
            raise ValueError("known interval must be half-open and positive")


@dataclass(frozen=True)
class SecurityStateVersion:
    state_id: str
    asset_id: str
    effective_from: date
    effective_to: date | None
    known_from: datetime
    known_to: datetime | None
    list_date: date | None
    delist_date: date | None
    name: str | None
    board: Board
    risk_status: RiskStatus
    suspended: bool | None
    source_ref: str
    confidence: Confidence
    revision: str

    def __post_init__(self) -> None:
        require_aware(self.known_from, "known_from")
        if self.known_to:
            require_aware(self.known_to, "known_to")
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective interval must be half-open and positive")
        if self.known_to and self.known_to <= self.known_from:
            raise ValueError("known interval must be half-open and positive")
        if self.list_date and self.delist_date and self.delist_date < self.list_date:
            raise ValueError("delist_date cannot precede list_date")
        if not self.source_ref or not self.revision:
            raise ValueError("source_ref and revision are required")

    def visible_at(self, effective_date: date, decision_time: datetime) -> bool:
        cutoff = require_aware(decision_time, "decision_time")
        return (
            self.effective_from <= effective_date
            and (self.effective_to is None or effective_date < self.effective_to)
            and self.known_from <= cutoff
            and (self.known_to is None or cutoff < self.known_to)
        )

    def trading_status(
        self, effective_date: date, *, bar_present: bool | None = None
    ) -> TradingStatus:
        if self.list_date is None or effective_date < self.list_date:
            return TradingStatus.NOT_LISTED
        if self.delist_date is not None and effective_date > self.delist_date:
            return TradingStatus.DELISTED
        if self.suspended is True:
            return TradingStatus.SUSPENDED
        # Missing a bar is evidence of missing data, not evidence of suspension.
        if self.suspended is None or bar_present is False:
            return TradingStatus.UNKNOWN
        return TradingStatus.TRADABLE
