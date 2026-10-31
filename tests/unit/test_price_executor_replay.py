"""Tests for the price executor replay validation logic.

Proves correctness of:
- Session-next resolution (weekday and weekend handling).
- Price history filtering to the ETF3 universe.
- Monthly NAV return aggregation.
- Replay pre-flight guards: unsupported strategy, incomplete sessions,
  insufficient warmup, too-few selected sessions, and future-data leakage.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.quantcore.research.price_executor import (
    ETF3,
    _all_history,
    _monthly_returns,
    _next_session_factory,
    _replay,
)
from src.quantcore.strategies.price import StrategySignal

SHANGHAI = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _business_days(start: date, count: int) -> list[date]:
    """Return *count* business days starting from *start*."""
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# _next_session_factory
# ---------------------------------------------------------------------------


class TestNextSessionFactory:
    """Proves that session adjacency is resolved correctly."""

    def test_known_successor(self) -> None:
        """Consecutive sessions in the supplied list map directly."""
        sessions = [date(2026, 1, 1), date(2026, 1, 5)]  # Thu -> Mon
        assert _next_session_factory(sessions)(date(2026, 1, 1)) == date(2026, 1, 5)

    def test_weekend_fallback(self) -> None:
        """Non-session dates advance to the next business day."""
        sessions = [date(2026, 1, 2)]  # Friday
        # Saturday Jan 3 -> skip to Monday Jan 5
        assert _next_session_factory(sessions)(date(2026, 1, 3)) == date(2026, 1, 5)


# ---------------------------------------------------------------------------
# _all_history
# ---------------------------------------------------------------------------


class TestAllHistory:
    """Proves that non-ETF3 rows are excluded from the price history."""

    def test_filters_to_etf3_universe(self) -> None:
        rows = [
            {"ts_code": "510300.SH", "trade_date": "2020-01-06", "close": "4.00", "vol": "100"},
            {"ts_code": "999999.SH", "trade_date": "2020-01-06", "close": "1.00", "vol": "50"},
        ]
        history = _all_history(rows)
        assert len(history) == 1
        assert history[0].asset_id == "510300.SH"
        assert history[0].close == Decimal("4.00")

    def test_empty_input(self) -> None:
        assert _all_history([]) == []


# ---------------------------------------------------------------------------
# _monthly_returns
# ---------------------------------------------------------------------------


class TestMonthlyReturns:
    """Proves the monthly-return aggregation from a NAV series."""

    def test_computes_monthly_return(self) -> None:
        nav = [("2020-01-06", Decimal("100")), ("2020-01-31", Decimal("110"))]
        result = _monthly_returns(nav)
        assert "2020-01" in result
        assert float(result["2020-01"]) == pytest.approx(0.1)

    def test_single_entry_month_is_none(self) -> None:
        """A month with only one NAV point has no return to compute."""
        nav = [("2020-01-06", Decimal("100"))]
        assert _monthly_returns(nav)["2020-01"] is None

    def test_empty_nav(self) -> None:
        assert _monthly_returns([]) == {}


# ---------------------------------------------------------------------------
# _replay validation guards
# ---------------------------------------------------------------------------


def _etf_rows(sessions: list[date], close: str = "4.00") -> list[dict]:
    """Build price/limit rows for all three ETFs across *sessions*."""
    rows: list[dict] = []
    for session in sessions:
        for code in ETF3:
            rows.append(
                {"ts_code": code, "trade_date": session.isoformat(), "close": close, "vol": "100"}
            )
    return rows


def _limit_rows(sessions: list[date]) -> list[dict]:
    rows: list[dict] = []
    for session in sessions:
        for code in ETF3:
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": session.isoformat(),
                    "up_limit": "4.40",
                    "down_limit": "3.60",
                }
            )
    return rows


class TestReplayValidation:
    """Proves that _replay rejects invalid inputs before any execution."""

    def test_rejects_unsupported_strategy(self) -> None:
        """A strategy not in the ETF3 catalog must be refused."""
        with pytest.raises(ValueError, match="not executable"):
            _replay("bogus.v1", [], [], date(2020, 1, 1), date(2020, 1, 3), Decimal("1000000"))

    def test_rejects_no_complete_sessions(self) -> None:
        """Sessions missing any ETF3 member are not usable."""
        sessions = _business_days(date(2020, 1, 6), 5)
        rows = _etf_rows(sessions)[:2]  # only two ETFs represented
        with pytest.raises(ValueError, match="no complete"):
            _replay("etf_rotation.v1", rows, _limit_rows(sessions), sessions[0], sessions[-1], Decimal("1000000"))

    def test_insufficient_warmup_raises(self) -> None:
        """etf_momentum_20.v1 needs 21 prior sessions; only 20 provided."""
        warmup = _business_days(date(2019, 1, 2), 20)
        selected = _business_days(date(2020, 1, 6), 3)
        all_sessions = warmup + selected
        with pytest.raises(ValueError, match="insufficient warmup"):
            _replay(
                "etf_momentum_20.v1",
                _etf_rows(all_sessions),
                _limit_rows(all_sessions),
                selected[0],
                selected[-1],
                Decimal("1000000"),
            )

    def test_rejects_too_few_selected_sessions(self) -> None:
        """The backtest range must contain at least three sessions."""
        warmup = _business_days(date(2019, 1, 2), 21)
        selected = _business_days(date(2020, 1, 6), 2)  # only two selected
        all_sessions = warmup + selected
        with pytest.raises(ValueError, match="at least three"):
            _replay(
                "etf_momentum_20.v1",
                _etf_rows(all_sessions),
                _limit_rows(all_sessions),
                selected[0],
                selected[-1],
                Decimal("1000000"),
            )

    def test_rejects_future_data_leakage(self) -> None:
        """Strategy evidence must not reference sessions after the decision time."""
        warmup = _business_days(date(2019, 1, 2), 21)
        selected = _business_days(date(2020, 1, 6), 4)
        all_sessions = warmup + selected

        future_signal = StrategySignal(
            strategy_id="etf_momentum_20",
            version="v1",
            decision_time=datetime.combine(selected[0], datetime(2020, 1, 1).time(), tzinfo=SHANGHAI),
            scores={"510300.SH": Decimal("0.1")},
            targets={"510300.SH": Decimal("1.0")},
            evidence_sessions={"510300.SH": date(2020, 1, 10)},  # future
        )
        with patch(
            "src.quantcore.research.price_executor.run_strategy",
            return_value=future_signal,
        ):
            with pytest.raises(RuntimeError, match="after its decision time"):
                _replay(
                    "etf_momentum_20.v1",
                    _etf_rows(all_sessions),
                    _limit_rows(all_sessions),
                    selected[0],
                    selected[-1],
                    Decimal("1000000"),
                )
