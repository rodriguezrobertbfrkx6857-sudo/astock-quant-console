from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.data.models import Provenance


@dataclass(frozen=True)
class Tick:
    source_id: str
    asset_id: str
    observed_at: str
    available_at: str
    price: Decimal
    volume: Decimal | None
    provenance: Provenance
    raw_object_hash: str

    def __post_init__(self) -> None:
        observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        available = datetime.fromisoformat(self.available_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or available.tzinfo is None:
            raise ValueError("tick timestamps must be timezone-aware")
        if available < observed:
            raise ValueError("available_at cannot precede observed_at")
        if self.price <= 0 or self.volume is not None and self.volume < 0:
            raise ValueError("invalid tick values")
        if len(self.raw_object_hash) != 64:
            raise ValueError("raw object hash is required")


class IntradayStore:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def append(self, tick: Tick) -> bool:
        if tick.provenance is not Provenance.REAL:
            raise ValueError(
                "live intraday store accepts REAL vendor observations only"
            )
        conn = sqlite3.connect(self.database)
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO intraday_ticks VALUES (?,?,?,?,?,?,?,?)",
                (
                    tick.source_id,
                    tick.asset_id,
                    tick.observed_at,
                    tick.available_at,
                    str(tick.price),
                    str(tick.volume) if tick.volume is not None else None,
                    tick.provenance.value,
                    tick.raw_object_hash,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()


class WatchEngine:
    """Deterministic alert evaluation; it never creates orders or fills."""

    @staticmethod
    def evaluate(
        previous: Tick | None,
        current: Tick,
        *,
        move_threshold: Decimal = Decimal("0.03"),
        volume_ratio: Decimal | None = None,
        volatility_ratio: Decimal | None = None,
        gap: Decimal | None = None,
        session_high: Decimal | None = None,
        session_low: Decimal | None = None,
        breakout_reference: Decimal | None = None,
        breakdown_reference: Decimal | None = None,
        limit_up: Decimal | None = None,
        limit_down: Decimal | None = None,
        limit_proximity: Decimal = Decimal("0.02"),
        factor_rank_before: int | None = None,
        factor_rank_after: int | None = None,
        strategy_state: str | None = None,
        paper_nav: Decimal | None = None,
        paper_peak_nav: Decimal | None = None,
        drawdown_warning: Decimal = Decimal("0.10"),
        risk_warning: str | None = None,
        new_event: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        alerts: list[dict[str, str]] = []
        if previous is not None:
            move = current.price / previous.price - Decimal("1")
            if abs(move) >= move_threshold:
                alerts.append(
                    {"type": "ABNORMAL_MOVE", "severity": "WARN", "value": str(move)}
                )
        if volume_ratio is not None and volume_ratio >= Decimal("3"):
            alerts.append(
                {
                    "type": "ABNORMAL_VOLUME",
                    "severity": "WARN",
                    "value": str(volume_ratio),
                }
            )
        if volatility_ratio is not None and volatility_ratio >= Decimal("2"):
            alerts.append(
                {
                    "type": "VOLATILITY_SPIKE",
                    "severity": "WARN",
                    "value": str(volatility_ratio),
                }
            )
        if gap is not None and abs(gap) >= move_threshold:
            alerts.append({"type": "GAP", "severity": "WARN", "value": str(gap)})
        if (
            breakout_reference is not None
            and session_high is not None
            and session_high > breakout_reference
        ):
            alerts.append(
                {"type": "BREAKOUT", "severity": "INFO", "value": str(session_high)}
            )
        if (
            breakdown_reference is not None
            and session_low is not None
            and session_low < breakdown_reference
        ):
            alerts.append(
                {"type": "BREAKDOWN", "severity": "WARN", "value": str(session_low)}
            )
        if (
            limit_up is not None
            and limit_up > 0
            and (limit_up - current.price) / limit_up <= limit_proximity
        ):
            alerts.append(
                {
                    "type": "PRICE_LIMIT_PROXIMITY_UP",
                    "severity": "WARN",
                    "value": str(current.price),
                }
            )
        if (
            limit_down is not None
            and limit_down > 0
            and (current.price - limit_down) / limit_down <= limit_proximity
        ):
            alerts.append(
                {
                    "type": "PRICE_LIMIT_PROXIMITY_DOWN",
                    "severity": "WARN",
                    "value": str(current.price),
                }
            )
        if factor_rank_before is not None and factor_rank_after is not None and factor_rank_before != factor_rank_after:
            alerts.append(
                {
                    "type": "FACTOR_RANK_CHANGE",
                    "severity": "INFO",
                    "value": f"{factor_rank_before}->{factor_rank_after}",
                }
            )
        if strategy_state:
            alerts.append(
                {"type": "STRATEGY_STATE", "severity": "INFO", "value": strategy_state}
            )
        if paper_nav is not None and paper_peak_nav is not None and paper_peak_nav > 0:
            drawdown = Decimal("1") - paper_nav / paper_peak_nav
            if drawdown >= drawdown_warning:
                alerts.append(
                    {"type": "PAPER_DRAWDOWN", "severity": "WARN", "value": str(drawdown)}
                )
        if risk_warning:
            alerts.append(
                {"type": "RISK_WARNING", "severity": "WARN", "value": risk_warning}
            )
        if new_event:
            alerts.append({"type": "NEW_EVENT", "severity": "INFO", "value": new_event})
        return tuple(alerts)

    @staticmethod
    def alert_id(tick: Tick, alert_type: str) -> str:
        return hashlib.sha256(
            f"{tick.source_id}|{tick.asset_id}|{tick.observed_at}|{alert_type}".encode()
        ).hexdigest()
