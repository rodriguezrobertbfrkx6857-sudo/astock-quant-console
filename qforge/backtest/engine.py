# -*- coding: utf-8 -*-
"""A-share backtest engine (event-driven daily loop).

Market rules implemented (T = signal day, T+1 = execution day):
- signals at T close -> orders execute no earlier than T+1 open (T+1 rule for
  signals; share settlement T+1 enforced on sells)
- lot size 100 shares for buys; sells may close odd lots
- price limits: vendor up/down limits with computed fallback (board & ST aware)
  buy blocked when open at/above up_limit; sell blocked at/below down_limit
- suspension: no fills, orders defer; delisting forces haircut settlement
- fees: commission (min), stamp tax (time-varying 2023-08-28 rule), transfer fee
- slippage on open price
- corporate actions: adj-factor ratio on ex-date (splits/bonus) + cash dividend
- risk gate: max position weight / max positions / min trade value / rebalance
  band / optional drawdown kill-switch

All data access goes through BarSource built from PIT-curated tables; the
engine never sees a bar beyond the current decision date.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import db
from ..config import Settings, get_settings
from ..data import calendar as cal
from ..data.pit import PITReader
from ..util import get_logger, now_ts, sha1
from ..data.build import limit_pct_for

log = get_logger("qforge.engine")

STAMP_CUT = "20230828"  # stamp tax cut date: sell 0.001 -> 0.0005


# ---------------------------------------------------------------- config ----
@dataclass
class EngineConfig:
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_sell: float = 0.0005
    stamp_sell_before_cut: float = 0.001
    transfer_fee: float = 0.00001
    slippage_bps: float = 10.0
    lot_size: int = 100
    max_position_weight: float = 0.15
    max_positions: int = 50
    min_trade_value: float = 1000.0
    rebalance_band: float = 0.005
    delist_haircut: float = 0.90
    order_max_age: int = 15
    dd_kill: float = 0.0
    tier: str = "production"
    bench_code: str = "000300.SH"

    @classmethod
    def from_settings(cls, s: Settings | None = None, **over) -> "EngineConfig":
        s = s or get_settings()
        base = dict(initial_cash=s.initial_cash, commission_rate=s.commission_rate,
                    commission_min=s.commission_min, stamp_sell=s.stamp_tax_sell,
                    transfer_fee=s.transfer_fee, slippage_bps=s.slippage_bps,
                    lot_size=s.lot_size, max_position_weight=s.max_position_weight,
                    max_positions=s.max_positions, min_trade_value=s.min_trade_value,
                    rebalance_band=s.rebalance_band, delist_haircut=s.delist_haircut,
                    dd_kill=s.risk_dd_kill)
        base.update(over)
        return cls(**base)


# ---------------------------------------------------------------- bar source ----
class BarSource:
    """In-memory pivot of curated bars + universe, bounded to <= end date.

    max_names caps the cross-section (top by 60d avg amount at each date --
    itself a PIT quantity) to keep research runs light.
    """

    def __init__(self, end: str, tier: str = "production", max_names: int | None = None,
                 start: str | None = None, codes: list[str] | tuple | set | None = None):
        s = get_settings()
        self.end = end
        self.tier = tier
        mk = db.connect_market()
        try:
            if codes:
                codes_list = list(codes)
                placeholders = ",".join("?" * len(codes_list))
                sql_bars = (f"SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, "
                            f"adj_factor, up_limit, down_limit, is_suspended, is_st, board "
                            f"FROM bars WHERE trade_date <= ? AND ts_code IN ({placeholders})")
                params_bars = [end, *codes_list]
                if start:
                    sql_bars += " AND trade_date >= ?"
                    params_bars.append(start)
            else:
                sql_bars = ("SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, "
                            "adj_factor, up_limit, down_limit, is_suspended, is_st, board "
                            "FROM bars NOT INDEXED WHERE trade_date <= ?")
                params_bars = [end]
                if start:
                    sql_bars += " AND trade_date >= ?"
                    params_bars.append(start)
            bars = pd.read_sql_query(sql_bars, mk, params=tuple(params_bars))
            delist = pd.read_sql_query(
                "SELECT ts_code, delist_date FROM sec_master WHERE sec_type='STOCK'", mk)
            sectype = pd.read_sql_query("SELECT ts_code, sec_type FROM sec_master", mk)
        finally:
            mk.close()
        self.delist = {r["ts_code"]: r["delist_date"] for r in delist.to_dict("records")}
        self.sec_type = dict(zip(sectype["ts_code"], sectype["sec_type"]))
        if bars.empty:
            for name in ("open", "close", "adj", "up_limit", "down_limit", "amount", "volume", "high", "low"):
                setattr(self, name, pd.DataFrame())
            self.suspended = set()
            self.st = set()
            self.board = {}
            self.universe_by_date = {}
            return
        if max_names and not codes:
            bars = self._cap_names(bars, max_names)
        cols = sorted(bars["ts_code"].unique())
        deduped = bars.drop_duplicates(subset=["trade_date", "ts_code"])
        piv = deduped.pivot(index="trade_date", columns="ts_code")
        self.open_ = piv["open"].reindex(columns=cols) if "open" in piv else pd.DataFrame(columns=cols)
        self.close = piv["close"].reindex(columns=cols) if "close" in piv else pd.DataFrame(columns=cols)
        self.adj = piv["adj_factor"].reindex(columns=cols).ffill() if "adj_factor" in piv else pd.DataFrame(columns=cols)
        self.amount = piv["amount"].reindex(columns=cols) if "amount" in piv else pd.DataFrame(columns=cols)
        self.up_limit = piv["up_limit"].reindex(columns=cols) if "up_limit" in piv else pd.DataFrame(columns=cols)
        self.down_limit = piv["down_limit"].reindex(columns=cols) if "down_limit" in piv else pd.DataFrame(columns=cols)
        self.high = piv["high"].reindex(columns=cols) if "high" in piv else pd.DataFrame(columns=cols)
        self.low = piv["low"].reindex(columns=cols) if "low" in piv else pd.DataFrame(columns=cols)
        self.suspended = set(map(tuple, bars[bars["is_suspended"] == 1][["trade_date", "ts_code"]].to_numpy()))
        self.st = set(map(tuple, bars[bars["is_st"] == 1][["trade_date", "ts_code"]].to_numpy()))
        self.board = dict(zip(bars["ts_code"], bars["board"]))
        # universe per date from curated univ_daily (never today's list)
        colname = "eligible_production" if tier == "production" else "eligible_research"
        mk = db.connect_market()
        try:
            if codes:
                codes_list = list(codes)
                placeholders = ",".join("?" * len(codes_list))
                sql_u = (f"SELECT trade_date, ts_code FROM univ_daily WHERE {colname}=1 AND trade_date<=? "
                         f"AND ts_code IN ({placeholders})")
                params_u = [end, *codes_list]
                if start:
                    sql_u += " AND trade_date >= ?"
                    params_u.append(start)
            else:
                sql_u = f"SELECT trade_date, ts_code FROM univ_daily WHERE {colname}=1 AND trade_date<=?"
                params_u = [end]
                if start:
                    sql_u += " AND trade_date >= ?"
                    params_u.append(start)
            u = pd.read_sql_query(sql_u, mk, params=tuple(params_u))
        finally:
            mk.close()
        self.universe_by_date: dict[str, set] = {}
        if not u.empty:
            for d, g in u.groupby("trade_date"):
                self.universe_by_date[d] = set(g["ts_code"])
        # keep only codes present in capped bars
        if max_names:
            allowed = set(cols)
            self.universe_by_date = {d: (u_codes & allowed) for d, u_codes in self.universe_by_date.items()}

    @staticmethod
    def _cap_names(bars: pd.DataFrame, max_names: int) -> pd.DataFrame:
        """Top max_names by trailing 60d average amount, per date (PIT-safe)."""
        amt = bars.pivot_table(index="trade_date", columns="ts_code", values="amount", aggfunc="last")
        avg = amt.rolling(60, min_periods=20).mean()
        first_dates = set(sorted(bars["trade_date"].unique())[:70])
        keep_rows = []
        for d, sub in bars.groupby("trade_date", sort=False):
            if d in first_dates:
                keep_rows.append(sub)
                continue
            if d not in avg.index:
                continue
            row = avg.loc[d].dropna()
            if len(row) <= max_names:
                keep_rows.append(sub)
            else:
                keep = set(row.nlargest(max_names).index)
                keep_rows.append(sub[sub["ts_code"].isin(keep)])
        if not keep_rows:
            return bars
        out = pd.concat(keep_rows, ignore_index=True)
        return out.drop_duplicates(subset=["trade_date", "ts_code"]).sort_values(["trade_date", "ts_code"])

    # accessors ----------------------------------------------------------
    def sessions(self) -> list[str]:
        if self.close.empty:
            return []
        return list(self.close.index)

    def bar(self, code: str, date: str) -> dict | None:
        if date not in self.close.index or code not in self.close.columns:
            return None
        c = self.close.at[date, code]
        if pd.isna(c):
            return None
        def g(df):
            v = df.at[date, code]
            return None if pd.isna(v) else float(v)
        return {"ts_code": code, "trade_date": date, "open": g(self.open_), "close": float(c),
                "high": g(self.high), "low": g(self.low),
                "adj_factor": g(self.adj), "amount": g(self.amount),
                "up_limit": g(self.up_limit), "down_limit": g(self.down_limit),
                "is_suspended": (date, code) in self.suspended,
                "is_st": (date, code) in self.st,
                "board": self.board.get(code, "OTHER")}

    def universe(self, date: str) -> set:
        return self.universe_by_date.get(date, set())


# ---------------------------------------------------------------- positions ----
@dataclass
class Position:
    ts_code: str
    shares: float = 0.0
    available: float = 0.0     # T+1 settled shares sellable today
    avg_cost: float = 0.0
    last_price: float = 0.0
    last_date: str | None = None

    def value(self) -> float:
        return self.shares * self.last_price


@dataclass
class Order:
    seq: int
    ts_code: str
    side: str                  # BUY / SELL
    shares: float | None       # None => sell everything available
    target_weight: float
    issued_date: str
    exec_date: str
    age: int = 0
    status: str = "PENDING"    # PENDING/FILLED/DEFERRED/CANCELLED/REJECTED
    note: str = ""
    value_delta: float = 0.0


@dataclass
class Fill:
    seq: int
    order_seq: int
    trade_date: str
    ts_code: str
    side: str
    shares: float
    price: float
    fee: float
    cash_after: float
    note: str = ""


# ---------------------------------------------------------------- engine ----
class BacktestEngine:
    def __init__(self, strategy, params: dict, start: str, end: str,
                 cfg: EngineConfig | None = None, max_names: int | None = None,
                 label: str = "", scope: str = "research", bars: BarSource | None = None):
        self.strategy = strategy
        self.params = dict(params)
        self.start, self.end = start, end
        self.cfg = cfg or EngineConfig.from_settings()
        self.scope = scope
        self.label = label
        self.bars = bars if bars is not None else BarSource(end=end, tier=self.cfg.tier, max_names=max_names)
        self.cash = self.cfg.initial_cash
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.daily: list[dict] = []
        self.pending: list[Order] = []
        self._seq = 0
        self.strategy_state: dict = {}
        self.killed = False
        self.peak_nav = self.cfg.initial_cash
        self.corp: dict[tuple, dict] = self._load_corp(end)

    # ------------------------------------------------------------------ setup
    def _load_corp(self, end: str) -> dict[tuple, dict]:
        mk = db.connect_market()
        try:
            rows = db.q(mk, "SELECT ts_code, ex_date, cash_div, stk_ratio, adj_before, adj_after "
                            "FROM corp_action WHERE ex_date <= ?", (end,))
        finally:
            mk.close()
        return {(r["ts_code"], r["ex_date"]): dict(r) for r in rows}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ fees
    def _fees(self, code: str, side: str, value: float, date: str) -> float:
        cfg = self.cfg
        fee = max(cfg.commission_min, cfg.commission_rate * value)
        if side == "SELL":
            stamp = cfg.stamp_sell_before_cut if date < STAMP_CUT else cfg.stamp_sell
            fee += stamp * value
        if code.endswith(".SH"):
            fee += cfg.transfer_fee * value
        return fee

    # ------------------------------------------------------------------ corporate actions
    def _apply_corp_actions(self, date: str) -> None:
        for code, pos in self.positions.items():
            ca = self.corp.get((code, date))
            if not ca or pos.shares <= 0:
                continue
            adj_before, adj_after = ca.get("adj_before"), ca.get("adj_after")
            if adj_before and adj_after and adj_before > 0:
                ratio = adj_after / adj_before
            else:
                ratio = 1.0 + float(ca.get("stk_ratio") or 0.0)
            cash_div = float(ca.get("cash_div") or 0.0)
            new_shares = pos.shares * ratio
            cash_in = pos.shares * cash_div
            pos.avg_cost = max(0.0, (pos.avg_cost / ratio) - cash_div) if ratio > 0 else pos.avg_cost
            pos.shares = new_shares
            # new shares from corporate actions settle T+1 as well
            pos.available = min(pos.available * ratio, pos.shares)
            if cash_in:
                self.cash += cash_in

    # ------------------------------------------------------------------ order planning
    def plan_orders(self, weights: dict[str, float], date: str, nav: float) -> None:
        cfg = self.cfg
        exec_date = cal.next_open(date)
        if exec_date is None:
            return
        targets: dict[str, float] = {}
        for code, w in weights.items():
            w = float(np.clip(w, 0.0, cfg.max_position_weight))
            targets[code] = w
        # sells first (free cash), then buys by size
        actions = []
        universe = self.bars.universe(date)
        all_codes = set(targets) | set(self.positions)
        for code in all_codes:
            w = targets.get(code, 0.0)
            bar = self.bars.bar(code, date)
            price = bar["close"] if bar else (self.positions[code].last_price if code in self.positions else None)
            if not price or price <= 0:
                continue
            cur_shares = self.positions[code].shares if code in self.positions else 0.0
            cur_value = cur_shares * price
            tgt_value = w * nav
            delta = tgt_value - cur_value
            if abs(delta) / max(nav, 1.0) < cfg.rebalance_band:
                continue
            if delta < -cfg.min_trade_value * 0.5:
                side, shares = "SELL", None if w == 0.0 else max(0.0, cur_shares - tgt_value / price)
                actions.append((0, code, side, shares, w, delta))
            elif delta > cfg.min_trade_value:
                tradable = code in universe or self.bars.sec_type.get(code) == "ETF"
                if self.cfg.tier == "production" and not tradable:
                    continue
                tgt_shares = np.floor(tgt_value / price / cfg.lot_size) * cfg.lot_size
                buy_shares = tgt_shares - cur_shares
                if buy_shares < cfg.lot_size:
                    continue
                if code not in self.positions and len(self.positions) >= cfg.max_positions:
                    continue
                actions.append((1, code, "BUY", buy_shares, w, delta))
        actions.sort(key=lambda x: (x[0], -abs(x[5])))
        for _, code, side, shares, w, delta in actions:
            self._seq += 1
            order = Order(seq=self._seq, ts_code=code, side=side, shares=shares,
                          target_weight=w, issued_date=date, exec_date=exec_date,
                          value_delta=delta)
            self.pending.append(order)
            self.orders.append(order)

    # ------------------------------------------------------------------ fills
    def _fill_orders(self, date: str) -> None:
        still_pending = []
        # sells first
        for order in sorted(self.pending, key=lambda o: 0 if o.side == "SELL" else 1):
            if order.exec_date != date:
                still_pending.append(order)
                continue
            res = self._attempt_fill(order, date)
            if res == "DEFER":
                order.age += 1
                if order.age > self.cfg.order_max_age:
                    order.status = "CANCELLED"
                    order.note = f"age>{self.cfg.order_max_age}"
                else:
                    still_pending.append(order)
            elif res == "FILLED":
                order.status = "FILLED"
            else:
                order.status = "REJECTED"
                order.note = res
        self.pending = still_pending

    def _attempt_fill(self, order: Order, date: str) -> str:
        bar = self.bars.bar(order.ts_code, date)
        if bar is None:
            return "DEFER" if self._maybe_alive(order.ts_code, date) else "REJECTED:no_bar"
        if bar["is_suspended"]:
            return "DEFER"
        slip = self.cfg.slippage_bps / 10000.0
        if order.side == "BUY":
            # re-check eligibility at fill time (state may have changed)
            if self.cfg.tier == "production" and self.bars.sec_type.get(order.ts_code) != "ETF":
                if order.ts_code not in self.bars.universe(date):
                    return "REJECTED:ineligible"
            up = bar["up_limit"]
            openp = bar["open"]
            if openp is None or up is None:
                return "DEFER"
            if openp >= up - 1e-9:      # limit-up open: cannot buy
                return "DEFER"
            price = min(openp * (1 + slip), up)
            shares = float(np.floor((order.shares or 0) / self.cfg.lot_size) * self.cfg.lot_size)
            if shares <= 0:
                return "REJECTED:zero_shares"
            while shares > 0:
                value = shares * price
                fee = self._fees(order.ts_code, "BUY", value, date)
                if value + fee <= self.cash:
                    break
                shares -= self.cfg.lot_size
            if shares <= 0:
                return "REJECTED:no_cash"
            value = shares * price
            fee = self._fees(order.ts_code, "BUY", value, date)
            self.cash -= value + fee
            pos = self.positions.setdefault(order.ts_code, Position(ts_code=order.ts_code))
            total_cost = pos.avg_cost * pos.shares + value + fee
            pos.shares += shares
            pos.avg_cost = total_cost / pos.shares if pos.shares else 0.0
            pos.last_price = price
            pos.last_date = date
            self.fills.append(Fill(seq=self._next_seq(), order_seq=order.seq, trade_date=date,
                                   ts_code=order.ts_code, side="BUY", shares=shares,
                                   price=price, fee=fee, cash_after=self.cash))
            return "FILLED"
        else:  # SELL
            pos = self.positions.get(order.ts_code)
            if not pos or pos.shares <= 0:
                return "REJECTED:no_position"
            dn = bar["down_limit"]
            openp = bar["open"]
            if openp is None:
                return "DEFER"
            if dn is not None and openp <= dn + 1e-9:  # limit-down open: cannot sell
                return "DEFER"
            price = max(openp * (1 - slip), dn) if dn is not None else openp * (1 - slip)
            avail = pos.available
            shares = min(avail, order.shares if order.shares is not None else avail)
            if shares <= 0:
                return "DEFER"
            value = shares * price
            fee = self._fees(order.ts_code, "SELL", value, date)
            self.cash += value - fee
            pos.shares -= shares
            pos.available -= shares
            pos.last_price = price
            pos.last_date = date
            self.fills.append(Fill(seq=self._next_seq(), order_seq=order.seq, trade_date=date,
                                   ts_code=order.ts_code, side="SELL", shares=shares,
                                   price=price, fee=fee, cash_after=self.cash))
            if pos.shares <= 1e-6:
                del self.positions[order.ts_code]
            return "FILLED"

    def _maybe_alive(self, code: str, date: str) -> bool:
        dd = self.bars.delist.get(code)
        if dd and date > dd:
            return False
        return True

    def _force_settle_delisted(self, date: str) -> None:
        for code in list(self.positions):
            pos = self.positions[code]
            dd = self.bars.delist.get(code)
            if not dd or date <= dd or pos.shares <= 0:
                continue
            px = pos.last_price * self.cfg.delist_haircut
            if px <= 0:
                px = 0.0
            value = pos.shares * px
            fee = self._fees(code, "SELL", value, date)
            self.cash += value - fee
            self.fills.append(Fill(seq=self._next_seq(), order_seq=0, trade_date=date,
                                   ts_code=code, side="SELL", shares=pos.shares, price=px,
                                   fee=fee, cash_after=self.cash, note="delist_forced"))
            log.warning("forced delist settlement %s on %s @%.3f", code, date, px)
            del self.positions[code]

    # ------------------------------------------------------------------ marking
    def _mark(self, date: str) -> float:
        total = self.cash
        for code, pos in self.positions.items():
            bar = self.bars.bar(code, date)
            if bar and bar["close"]:
                pos.last_price = bar["close"]
                pos.last_date = date
            total += pos.shares * pos.last_price
        return total

    # ------------------------------------------------------------------ main loop
    def run(self) -> "BacktestResult":
        sessions = [d for d in self.bars.sessions() if self.start <= d <= self.end]
        if not sessions:
            raise RuntimeError(f"no sessions in [{self.start}, {self.end}] (data missing?)")
        bench_close = self._bench_series(sessions)
        # settle T+1 availability at each day start
        for date in sessions:
            for pos in self.positions.values():
                pos.available = pos.shares
            self._apply_corp_actions(date)
            self._force_settle_delisted(date)
            self._fill_orders(date)
            nav = self._mark(date)
            self.peak_nav = max(self.peak_nav, nav)
            if self.cfg.dd_kill and nav < self.peak_nav * (1 - self.cfg.dd_kill):
                self.killed = True
            self.daily.append({"trade_date": date, "nav": nav, "cash": self.cash,
                               "n_pos": len(self.positions),
                               "exposure": 1.0 - self.cash / nav if nav else 0.0})
            # signals at close of T (executed T+1 by construction)
            if not self.killed:
                pit = PITReader(date, strict=True)
                try:
                    weights = self.strategy.target_weights(
                        StrategyContext(date=date, nav=nav, pit=pit, bars=self.bars,
                                        positions=self.positions, cfg=self.cfg, engine=self,
                                        state=self.strategy_state),
                        self.params)
                except Exception as e:
                    log.exception("strategy %s failed on %s", self.strategy.strategy_id, date)
                    weights = {}
                if weights:
                    self.plan_orders(weights, date, nav)
        res = BacktestResult(engine=self, bench_close=bench_close)
        return res

    def _bench_series(self, sessions: list[str]) -> pd.Series:
        mk = db.connect_market()
        try:
            rows = db.q(mk, "SELECT trade_date, close FROM bench_daily WHERE ts_code=? AND trade_date>=? AND trade_date<=?",
                        (self.cfg.bench_code, sessions[0], sessions[-1]))
        finally:
            mk.close()
        s = pd.Series({r["trade_date"]: r["close"] for r in rows}, dtype=float)
        return s.reindex(sessions).ffill()


@dataclass
class StrategyContext:
    date: str
    nav: float
    pit: PITReader
    bars: BarSource
    positions: dict
    cfg: EngineConfig
    engine: "BacktestEngine"
    state: dict = field(default_factory=dict)


class BaseStrategy:
    strategy_id = "BASE"
    name = ""
    description = ""
    category = ""
    default_params: dict = {}
    required_factors: tuple = ()
    required_datasets: tuple = ("daily",)

    def target_weights(self, ctx: StrategyContext, params: dict) -> dict[str, float]:
        raise NotImplementedError

    def rebalance_due(self, ctx: StrategyContext, params: dict) -> bool:
        raise NotImplementedError


@dataclass
class BacktestResult:
    engine: BacktestEngine
    bench_close: pd.Series

    def __post_init__(self):
        d = pd.DataFrame(self.engine.daily).set_index("trade_date")
        if not d.empty:
            d["ret"] = d["nav"].pct_change().fillna(0.0)
            b = self.bench_close
            d["bench_ret"] = b.pct_change().fillna(0.0) if not b.empty else 0.0
            d["bench_nav"] = (1 + d["bench_ret"]).cumprod() if not b.empty else 1.0
        self.nav = d

    @property
    def orders_df(self) -> pd.DataFrame:
        rows = [vars(x) for x in self.engine.orders]
        return pd.DataFrame(rows)

    @property
    def fills_df(self) -> pd.DataFrame:
        rows = [vars(f) for f in self.engine.fills]
        return pd.DataFrame(rows)

    @property
    def fees_total(self) -> float:
        f = self.fills_df
        return float(f["fee"].sum()) if not f.empty else 0.0

    def turnover_value(self) -> float:
        f = self.fills_df
        return float((f["shares"] * f["price"]).abs().sum()) if not f.empty else 0.0

    def summary(self) -> dict:
        from ..research.metrics import perf_stats
        return perf_stats(self.nav, self.fills_df, self.engine.cfg.initial_cash)
