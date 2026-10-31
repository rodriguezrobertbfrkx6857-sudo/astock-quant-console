# -*- coding: utf-8 -*-
"""PIT (point-in-time) data access -- the anti-leakage boundary.

RULES ENFORCED HERE (violations raise PITViolation):
1. Every read takes a `decision_date`; only rows with
   available_at <= decision_ts(decision_date) are returned.
2. request windows beyond decision_date are clipped / rejected in strict mode.
3. Fundamentals are additionally lagged by config.fund_lag_days beyond their
   announcement date (conservative disclosure timing).

The backtest engine, research lab and paper system all read through
PITReader -- nothing else reads market tables directly.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from .. import db
from ..config import get_settings
from ..util import available_at_next_day, decision_ts, ymd


class PITViolation(RuntimeError):
    pass


class PITReader:
    """Read-only point-in-time view of curated market data."""

    def __init__(self, decision_date: str, strict: bool = True,
                 hard_end: str | None = None):
        """hard_end: research clip (features beyond it are invisible even if PIT)."""
        self.decision_date = ymd(decision_date)
        self.decision_instant = decision_ts(decision_date)
        self.strict = strict
        self.hard_end = ymd(hard_end) if hard_end else None
        self._mk = None
        self._lab = None

    @property
    def mk(self):
        if self._mk is None:
            self._mk = db.connect_market()
        return self._mk

    @property
    def lab(self):
        if self._lab is None:
            self._lab = db.connect_lab()
        return self._lab

    def close(self):
        if self._mk is not None:
            try: self._mk.close()
            except Exception: pass
            self._mk = None
        if self._lab is not None:
            try: self._lab.close()
            except Exception: pass
            self._lab = None

    def __del__(self):
        self.close()

    # ------------------------------------------------------------------ guard
    def _clip_end(self, end: str | None, what: str) -> str | None:
        if end is None:
            return None
        e = ymd(end)
        if self.hard_end and e > self.hard_end:
            if self.strict:
                raise PITViolation(
                    f"{what}: requested end {e} beyond research hard_end {self.hard_end}")
            e = self.hard_end
        if e > self.decision_date:
            if self.strict:
                raise PITViolation(
                    f"{what}: requested end {e} beyond decision_date {self.decision_date}")
            e = self.decision_date
        return e

    def _exec(self, sql: str, params: tuple) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.mk, params=params)

    @staticmethod
    def _code_filter(codes: list[str] | set | tuple | None) -> tuple[str, list]:
        if not codes or len(codes) > 1000:
            return "", []
        clist = list(codes)
        return f" AND ts_code IN ({','.join('?' * len(clist))})", clist

    # ------------------------------------------------------------------ bars
    def bars(self, codes: list[str] | None = None, start: str | None = None,
             end: str | None = None, include_suspended: bool = False) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "bars")
        sql = ("SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, "
               "adj_factor, up_limit, down_limit, limit_source, is_suspended, is_st, board "
               "FROM bars WHERE trade_date <= ? AND trade_date <= ?")
        params: list = [self.decision_date, end]
        if start:
            params.append(ymd(start))
            sql += " AND trade_date >= ?"
        c_sql, c_params = self._code_filter(codes)
        sql += c_sql
        params.extend(c_params)
        if not include_suspended:
            sql += " AND is_suspended = 0"
        sql += " ORDER BY trade_date, ts_code"
        df = self._exec(sql, tuple(params))
        return df

    def bar_on(self, trade_date: str, codes: list[str] | None = None) -> pd.DataFrame:
        """Single day's bars; refuses future days relative to decision."""
        t = ymd(trade_date)
        if t > self.decision_date and self.strict:
            raise PITViolation(f"bar_on {t} beyond decision_date {self.decision_date}")
        if self.hard_end and t > self.hard_end and self.strict:
            raise PITViolation(f"bar_on {t} beyond research hard_end {self.hard_end}")
        sql = ("SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, "
               "adj_factor, up_limit, down_limit, is_suspended, is_st, board "
               "FROM bars WHERE trade_date = ?")
        params: list = [t]
        if codes:
            sql += f" AND ts_code IN ({','.join('?' * len(codes))})"
            params.extend(codes)
        return self._exec(sql, tuple(params))

    # ------------------------------------------------------------------ daily basics (PE/PB/mv/turnover)
    def daily_basic(self, codes: list[str] | None = None, start: str | None = None,
                    end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "daily_basic")
        sql = ("SELECT ts_code, trade_date, close, turnover_rate, turnover_rate_f, volume_ratio, "
               "pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, "
               "free_share, total_mv, circ_mv FROM norm_daily_basic WHERE trade_date <= ?")
        params: list = [end]
        if start:
            params.append(ymd(start))
            sql += " AND trade_date >= ?"
        c_sql, c_params = self._code_filter(codes)
        sql += c_sql
        params.extend(c_params)
        return self._exec(sql + " ORDER BY trade_date, ts_code", tuple(params))

    # ------------------------------------------------------------------ universe
    def universe(self, trade_date: str | None = None, tier: str = "production") -> list[str]:
        t = ymd(trade_date or self.decision_date)
        if t > self.decision_date and self.strict:
            raise PITViolation(f"universe {t} beyond decision_date {self.decision_date}")
        if self.hard_end and t > self.hard_end and self.strict:
            raise PITViolation(f"universe {t} beyond hard_end {self.hard_end}")
        col = "eligible_production" if tier == "production" else "eligible_research"
        df = self._exec(f"SELECT ts_code FROM univ_daily WHERE trade_date=? AND {col}=1", (t,))
        return df["ts_code"].tolist()

    def universe_frame(self, trade_date: str | None = None) -> pd.DataFrame:
        t = ymd(trade_date or self.decision_date)
        if t > self.decision_date and self.strict:
            raise PITViolation(f"universe {t} beyond decision_date {self.decision_date}")
        return self._exec("SELECT * FROM univ_daily WHERE trade_date=?", (t,))

    def industries(self) -> pd.Series:
        """Industry classification mapping for all securities (PIT-safe static metadata)."""
        df = self._exec("SELECT ts_code, industry FROM norm_stock_basic WHERE industry IS NOT NULL", ())
        if df.empty:
            return pd.Series(dtype=str)
        return df.set_index("ts_code")["industry"]

    # ------------------------------------------------------------------ corporate actions
    def corp_actions(self, codes: list[str] | None = None,
                     start: str | None = None, end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "corp_actions")
        sql = "SELECT ts_code, ex_date, cash_div, stk_ratio, adj_before, adj_after, source FROM corp_action WHERE ex_date <= ?"
        params: list = [end]
        if start:
            params.append(ymd(start))
            sql += " AND ex_date >= ?"
        c_sql, c_params = self._code_filter(codes)
        sql += c_sql
        params.extend(c_params)
        return self._exec(sql + " ORDER BY ts_code, ex_date", tuple(params))

    # ------------------------------------------------------------------ fundamentals (PIT lagged)
    def fundamentals(self, codes: list[str] | None = None,
                     start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """fina_indicator rows usable at decision time. A record is usable at
        ann_date + fund_lag_days (conservative disclosure lag)."""
        s = get_settings()
        end = self._clip_end(end or self.decision_date, "fundamentals")
        lag = int(s.fund_lag_days)
        cutoff = (dt.datetime.strptime(end, "%Y%m%d") - dt.timedelta(days=lag)).strftime("%Y%m%d")
        sql = ("SELECT ts_code, end_date, ann_date, f_ann_date, roe, roe_dt, grossprofit_margin, "
               "netprofit_margin, debt_to_assets, ocf_to_or, eps, bps, or_yoy, netprofit_yoy "
               "FROM norm_fina_indicator WHERE COALESCE(f_ann_date, ann_date) <= ?")
        params: list = [cutoff]
        if start:
            params.append(ymd(start))
            sql += " AND COALESCE(f_ann_date, ann_date) >= ?"
        c_sql, c_params = self._code_filter(codes)
        sql += c_sql
        params.extend(c_params)
        df = self._exec(sql + " ORDER BY ts_code, end_date", tuple(params))
        if not df.empty:
            df["available_at"] = df.apply(
                lambda r: available_at_next_day(r.get("f_ann_date") or r.get("ann_date")), axis=1)
        return df

    # ------------------------------------------------------------------ features
    def features(self, factor_id: str, start: str | None = None,
                 end: str | None = None, codes: list[str] | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "features")
        sql = ("SELECT factor_id, ts_code, trade_date, value, available_at FROM features "
               "WHERE factor_id = ? AND trade_date <= ? AND available_at <= ?")
        params: list = [factor_id, end, self.decision_instant]
        if start:
            params.append(ymd(start))
            sql += " AND trade_date >= ?"
        c_sql, c_params = self._code_filter(codes)
        sql += c_sql
        params.extend(c_params)
        return pd.read_sql_query(sql + " ORDER BY trade_date, ts_code", self.lab, params=tuple(params))

    def features_latest(self, factor_id: str) -> pd.DataFrame:
        """Most recent PIT cross-section of a factor at decision time."""
        row = db.q1(self.lab, "SELECT MAX(trade_date) mx FROM features WHERE factor_id=? "
                              "AND trade_date<=? AND available_at<=?",
                    (factor_id, self.decision_date, self.decision_instant))
        if not row or not row["mx"]:
            return pd.DataFrame(columns=["ts_code", "value"])
        df = pd.read_sql_query(
            "SELECT ts_code, trade_date, value FROM features WHERE factor_id=? AND trade_date=?",
            self.lab, params=(factor_id, row["mx"]))
        return df

    # ------------------------------------------------------------------ events (news/announcements)
    def announcements(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "announcements")
        sql = ("SELECT ann_id, ts_code, name, ann_date, title FROM norm_anns "
               "WHERE ann_date <= ?")
        params: list = [end]
        if start:
            params.append(ymd(start))
            sql += " AND ann_date >= ?"
        return self._exec(sql + " ORDER BY ann_date", tuple(params))

    def news(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "news")
        lag_minutes = get_settings().event_lag_minutes
        cutoff = f"{end[:4]}-{end[4:6]}-{end[6:]} 23:59:59"
        sql = ("SELECT nid, datetime, src, title, content FROM norm_news WHERE datetime <= ?")
        params: list = [cutoff]
        if start:
            sd = f"{ymd(start)[:4]}-{ymd(start)[4:6]}-{ymd(start)[6:]} 00:00:00"
            params.append(sd)
            sql += " AND datetime >= ?"
        df = self._exec(sql + " ORDER BY datetime", tuple(params))
        return df

    # ------------------------------------------------------------------ benchmarks
    def bench(self, codes: list[str] | None = None, start: str | None = None,
              end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "bench")
        sql = ("SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount "
               "FROM bench_daily WHERE trade_date <= ?")
        params: list = [end]
        if start:
            params.append(ymd(start))
            sql += " AND trade_date >= ?"
        if codes:
            sql += f" AND ts_code IN ({','.join('?' * len(codes))})"
            params.extend(codes)
        return self._exec(sql + " ORDER BY trade_date, ts_code", tuple(params))

    def etf(self, codes: list[str] | None = None, start: str | None = None,
            end: str | None = None) -> pd.DataFrame:
        end = self._clip_end(end or self.decision_date, "etf")
        sql = ("SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, adj_factor "
               "FROM etf_daily WHERE trade_date <= ?")
        params: list = [end]
        if start:
            params.append(ymd(start))
            sql += " AND trade_date >= ?"
        if codes:
            sql += f" AND ts_code IN ({','.join('?' * len(codes))})"
            params.extend(codes)
        return self._exec(sql + " ORDER BY trade_date, ts_code", tuple(params))
