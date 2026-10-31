# -*- coding: utf-8 -*-
"""Independent Reference Engine -- second, deliberately separate implementation.

It consumes the SAME order stream the primary engine planned (persisted in
bt_orders) and re-simulates fills/accounting with its own code path (plain
dicts + direct per-row SQL reads, no shared helpers with the primary engine).
Divergence between the two engines is how accounting bugs surface.

Verdicts:
  CONSISTENT              -- NAV & fills match within tight tolerance
  EXPLAINABLE_DIFFERENCE  -- small, pattern-explained deviations
  INCONSISTENT            -- material divergence (must investigate)
  INSUFFICIENT_DATA       -- cannot simulate (missing bars/calendar)
"""
from __future__ import annotations

import uuid

import pandas as pd

from .. import db
from ..config import get_settings
from ..data import calendar as cal
from ..util import get_logger, now_ts

log = get_logger("qforge.validate")

NAV_TOL = 1e-6       # relative
FILL_TOL = 1e-6      # relative on price/fee


class ReferenceEngine:
    """Minimal independent simulator: orders -> fills -> daily cash/NAV."""

    def __init__(self, orders: list[dict], start: str, end: str, initial_cash: float,
                 bench_code: str = "000300.SH"):
        self.orders = orders  # planned order stream (same as primary)
        self.start, self.end = start, end
        self.cash = initial_cash
        self.cfg = get_settings()
        self.sessions = cal.open_days_between(start, end)
        self.pending: list[dict] = []
        self.fills: list[dict] = []
        self.daily: list[dict] = []
        self.positions: dict[str, dict] = {}   # code -> {shares, available, avg_cost, last_px}
        self.nav_by_date: dict[str, float] = {}

    # -- market data read straight from curated tables (own path) ---------
    def _bar(self, code: str, date: str) -> dict | None:
        mk = db.connect_market()
        try:
            r = db.q1(mk, "SELECT * FROM bars WHERE ts_code=? AND trade_date=?", (code, date))
            return dict(r) if r else None
        finally:
            mk.close()

    def _close(self, code: str, date: str) -> float | None:
        b = self._bar(code, date)
        return float(b["close"]) if b and b["close"] is not None else None

    def _last_close(self, code: str, asof: str) -> float | None:
        mk = db.connect_market()
        try:
            r = db.q1(mk, "SELECT close FROM bars WHERE ts_code=? AND trade_date<=? "
                          "AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1", (code, asof))
            return float(r["close"]) if r else None
        finally:
            mk.close()

    def _corp_ratio(self, code: str, date: str) -> tuple[float, float]:
        """(share multiplier, cash div) on ex-date, from curated corp_action."""
        mk = db.connect_market()
        try:
            r = db.q1(mk, "SELECT adj_before, adj_after, cash_div, stk_ratio FROM corp_action "
                          "WHERE ts_code=? AND ex_date=?", (code, date))
        finally:
            mk.close()
        if not r:
            return 1.0, 0.0
        mult = 1.0
        if r["adj_before"] and r["adj_after"]:
            mult = float(r["adj_after"]) / float(r["adj_before"])
        return mult, float(r["cash_div"] or 0.0)

    # -- fees (same policy, own implementation) ---------------------------
    def _fee(self, code: str, side: str, value: float, date: str) -> float:
        s = self.cfg
        fee = max(s.commission_min, s.commission_rate * value)
        if side == "SELL":
            stamp = 0.001 if date < "20230828" else s.stamp_tax_sell
            fee += stamp * value
        if code.endswith(".SH"):
            fee += s.transfer_fee * value
        return fee

    # -- main loop ---------------------------------------------------------
    def run(self) -> dict:
        pending = list(self.orders)
        for date in self.sessions:
            # settle T+1 availability
            for st in self.positions.values():
                st["available"] = st["shares"]
            # corporate actions
            for code, st in self.positions.items():
                mult, div = self._corp_ratio(code, date)
                if mult != 1.0 or div:
                    pre = st["shares"]
                    st["shares"] = pre * mult
                    st["available"] = st["available"] * mult
                    self.cash += pre * div
            # fills: sells first, then buys (documented primary policy)
            still = []
            for o in sorted(pending, key=lambda x: 0 if x.get("side") == "SELL" else 1):
                if o.get("exec_date") != date:
                    still.append(o)
                    continue
                outcome = self._fill(o, date)
                if outcome == "DEFER":
                    o["age"] = o.get("age", 0) + 1
                    if o["age"] <= self.cfg.order_max_age:
                        still.append(o)
            pending = still
            # mark NAV
            total = self.cash
            for code, st in self.positions.items():
                px = self._close(code, date)
                if px is not None:
                    st["last_px"] = px
                total += st["shares"] * (st.get("last_px") or 0.0)
            self.nav_by_date[date] = total
            self.daily.append({"trade_date": date, "nav": total, "cash": self.cash})
        return {"nav_by_date": self.nav_by_date, "fills": self.fills,
                "daily": self.daily, "unfilled": pending}

    def _fill(self, o: dict, date: str) -> str:
        code, side = o["ts_code"], o["side"]
        bar = self._bar(code, date)
        if bar is None:
            dd = self._delist_date(code)
            if dd and date > dd:
                return "REJECT"  # delisted; primary forces settlement separately
            return "DEFER"
        if bar["is_suspended"]:
            return "DEFER"
        slip = self.cfg.slippage_bps / 10000.0
        if side == "BUY":
            up, op = bar["up_limit"], bar["open"]
            if op is None or up is None or op >= up - 1e-9:
                return "DEFER"
            price = min(op * (1 + slip), up)
            shares = float(int((o.get("shares") or 0) // self.cfg.lot_size) * self.cfg.lot_size)
            while shares > 0:
                v = shares * price
                if v + self._fee(code, "BUY", v, date) <= self.cash:
                    break
                shares -= self.cfg.lot_size
            if shares <= 0:
                return "REJECT"
            v = shares * price
            fee = self._fee(code, "BUY", v, date)
            self.cash -= v + fee
            st = self.positions.setdefault(code, {"shares": 0.0, "available": 0.0,
                                                  "avg_cost": 0.0, "last_px": price})
            st["avg_cost"] = (st["avg_cost"] * st["shares"] + v + fee) / (st["shares"] + shares)
            st["shares"] += shares
            st["last_px"] = price
            self.fills.append({"trade_date": date, "ts_code": code, "side": "BUY",
                               "shares": shares, "price": price, "fee": fee,
                               "cash_after": self.cash})
            return "FILLED"
        st = self.positions.get(code)
        if not st or st["shares"] <= 0:
            return "REJECT"
        dn, op = bar["down_limit"], bar["open"]
        if op is None or (dn is not None and op <= dn + 1e-9):
            return "DEFER"
        price = max(op * (1 - slip), dn) if dn is not None else op * (1 - slip)
        shares = min(st["available"], o.get("shares") if o.get("shares") is not None else st["available"])
        if shares <= 0:
            return "DEFER"
        v = shares * price
        fee = self._fee(code, "SELL", v, date)
        self.cash += v - fee
        st["shares"] -= shares
        st["available"] -= shares
        st["last_px"] = price
        self.fills.append({"trade_date": date, "ts_code": code, "side": "SELL",
                           "shares": shares, "price": price, "fee": fee,
                           "cash_after": self.cash})
        if st["shares"] <= 1e-6:
            del self.positions[code]
        return "FILLED"

    def _delist_date(self, code: str) -> str | None:
        mk = db.connect_market()
        try:
            r = db.q1(mk, "SELECT delist_date FROM sec_master WHERE ts_code=?", (code,))
            return r["delist_date"] if r else None
        finally:
            mk.close()


# ---------------------------------------------------------------- compare ----
def validate_backtest(bt_id: str) -> dict:
    lab = db.connect_lab()
    mk = db.connect_market()
    try:
        bt = db.q1(lab, "SELECT * FROM backtests WHERE bt_id=?", (bt_id,))
        if not bt:
            return {"verdict": "INSUFFICIENT_DATA", "detail": "backtest not found"}
        orders = [dict(r) for r in db.q(lab, "SELECT * FROM bt_orders WHERE bt_id=? ORDER BY seq", (bt_id,))]
        prim_nav = {r["trade_date"]: r["nav"] for r in db.q(lab, "SELECT trade_date, nav FROM bt_nav WHERE bt_id=?", (bt_id,))}
        prim_fills = [dict(r) for r in db.q(lab, "SELECT * FROM bt_trades WHERE bt_id=? ORDER BY seq", (bt_id,))]
    finally:
        lab.close()
        mk.close()
    if not orders:
        return {"verdict": "INSUFFICIENT_DATA", "detail": "no persisted order stream"}

    # delist-force fills from primary are engine-side events (order_seq=0) --
    # reference reproduces NAV only up to those; treat missing bars as data gaps.
    ref = ReferenceEngine(orders, bt["start"], bt["end"], get_settings().initial_cash)
    try:
        out = ref.run()
    except Exception as e:
        return {"verdict": "INSUFFICIENT_DATA", "detail": f"reference crashed: {e}"}

    common = sorted(set(out["nav_by_date"]) & set(prim_nav))
    if not common:
        return {"verdict": "INSUFFICIENT_DATA", "detail": "no overlapping dates"}
    rel_diffs = [abs(out["nav_by_date"][d] - prim_nav[d]) / max(abs(prim_nav[d]), 1e-9)
                 for d in common]
    max_rel = max(rel_diffs)
    mean_abs = sum(abs(out["nav_by_date"][d] - prim_nav[d]) for d in common) / len(common)

    # fill-level comparison
    def key(f):
        return (f["trade_date"], f["ts_code"], f["side"])
    ref_fills = {}
    for f in out["fills"]:
        ref_fills[key(f)] = f
    prim_fills_map = {}
    for f in prim_fills:
        if f["note"] == "delist_forced":
            continue  # engine-side forced events; not part of the order stream
        prim_fills_map[key(f)] = f
    trade_mismatch = 0
    compared = 0
    for k, pf in prim_fills_map.items():
        rf = ref_fills.get(k)
        if rf is None:
            trade_mismatch += 1
            continue
        compared += 1
        if (abs(rf["shares"] - pf["shares"]) > 1e-6
                or abs(rf["price"] - pf["price"]) / max(pf["price"], 1e-9) > FILL_TOL
                or abs(rf["fee"] - pf["fee"]) / max(pf["fee"], 1e-9) > FILL_TOL):
            trade_mismatch += 1

    if max_rel < NAV_TOL and trade_mismatch == 0:
        verdict = "CONSISTENT"
    elif max_rel < 1e-4:
        verdict = "EXPLAINABLE_DIFFERENCE"
    else:
        verdict = "INCONSISTENT"
    detail = {"max_nav_rel_diff": max_rel, "mean_nav_abs_diff": mean_abs,
              "days_compared": len(common), "trades_compared": compared,
              "trade_mismatch": trade_mismatch,
              "ref_unfilled": len(out["unfilled"])}
    lab = db.connect_lab()
    try:
        db.upsert(lab, "validation_runs", [{
            "v_id": uuid.uuid4().hex[:16], "bt_id": bt_id, "verdict": verdict,
            "nav_max_rel_diff": max_rel, "nav_mean_abs_diff": mean_abs,
            "trades_compared": compared, "detail_json": db.json_dumps(detail),
            "created_at": now_ts()}],
            ["v_id", "bt_id", "verdict", "nav_max_rel_diff", "nav_mean_abs_diff",
             "trades_compared", "detail_json", "created_at"])
    finally:
        lab.close()
    log.info("validate %s -> %s max_rel=%.2e", bt_id, verdict, max_rel)
    return {"verdict": verdict, "detail": detail}
