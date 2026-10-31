# -*- coding: utf-8 -*-
"""PAPER trading system -- simulation only.

SAFETY: this module has no broker connectivity, no account access and no
order routing of any kind. It writes a local paper ledger (lab.db) and
emits only decision labels:
    BUY CANDIDATE / SELL CANDIDATE / HOLD / NO TRADE

Only strategies whose rating reached PAPER/CANDIDATE are admitted (Gate).

Daily EOD step for session D (runs without realtime data or LLM):
  1. fill orders issued for D using D's real open prices (T+1 rules)
  2. mark NAV at D close, write paper_nav
  3. run active strategies at D close -> paper_signals + orders for D+1
"""
from __future__ import annotations

import uuid

from .. import db
from ..backtest.engine import BarSource, EngineConfig
from ..config import get_settings
from ..data import calendar as cal
from ..data.pit import PITReader
from ..strategies import REGISTRY as STRATS
from ..util import get_logger, now_ts

log = get_logger("qforge.paper")

INITIAL_CASH = 1_000_000.0


# ------------------------------------------------------------------ gate ----
def active_strategies() -> list[str]:
    """Strategies allowed into PAPER by the rating gate."""
    lab = db.connect_lab()
    try:
        rows = db.q(lab, "SELECT strategy_id, status FROM strategy_registry "
                         "WHERE status IN ('PAPER','CANDIDATE')")
        return [r["strategy_id"] for r in rows]
    finally:
        lab.close()


def ensure_account(strategy_id: str) -> None:
    lab = db.connect_lab()
    try:
        if not db.q1(lab, "SELECT 1 FROM paper_account WHERE strategy_id=?", (strategy_id,)):
            db.upsert(lab, "paper_account", [{
                "strategy_id": strategy_id, "cash": INITIAL_CASH, "nav": INITIAL_CASH,
                "initial_cash": INITIAL_CASH, "updated_at": now_ts(),
                "status": "ACTIVE", "started_at": now_ts()}],
                ["strategy_id", "cash", "nav", "initial_cash", "updated_at", "status", "started_at"])
            _log(strategy_id, "INFO", f"paper account opened with {INITIAL_CASH:,.0f}")
    finally:
        lab.close()


def _log(strategy_id: str, level: str, msg: str) -> None:
    lab = db.connect_lab()
    try:
        lab.execute("INSERT INTO paper_log (strategy_id, ts, level, message) VALUES (?,?,?,?)",
                    (strategy_id, now_ts(), level, msg))
    finally:
        lab.close()


# ------------------------------------------------------------------ state ----
def _load_state(strategy_id: str) -> dict:
    lab = db.connect_lab()
    try:
        acc = db.q1(lab, "SELECT * FROM paper_account WHERE strategy_id=?", (strategy_id,))
        positions = {r["ts_code"]: {"shares": r["shares"], "available": r["available_shares"],
                                    "avg_cost": r["avg_cost"], "last_px": r["last_price"] or 0.0}
                     for r in db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (strategy_id,))}
        pending = [dict(r) for r in db.q(lab, "SELECT * FROM paper_orders WHERE strategy_id=? "
                                                "AND status='PENDING'", (strategy_id,))]
        state_json = db.kv_get(lab, f"paper_state:{strategy_id}", "{}")
    finally:
        lab.close()
    import json
    return {"cash": acc["cash"], "positions": positions, "pending": pending,
            "strategy_state": json.loads(state_json or "{}")}


def _save_state(strategy_id: str, cash: float, positions: dict, strategy_state: dict) -> None:
    lab = db.connect_lab()
    try:
        with db.tx(lab):
            for code, st in positions.items():
                lab.execute("INSERT OR REPLACE INTO paper_positions "
                            "(strategy_id, ts_code, shares, avg_cost, available_shares, last_price, updated_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (strategy_id, code, st["shares"], st["avg_cost"],
                             st["available"], st.get("last_px"), now_ts()))
            # delete closed positions
            held = set(positions)
            for r in db.q(lab, "SELECT ts_code FROM paper_positions WHERE strategy_id=?", (strategy_id,)):
                if r["ts_code"] not in held:
                    lab.execute("DELETE FROM paper_positions WHERE strategy_id=? AND ts_code=?",
                                (strategy_id, r["ts_code"]))
            lab.execute("UPDATE paper_account SET cash=?, updated_at=? WHERE strategy_id=?",
                        (cash, now_ts(), strategy_id))
        db.kv_set(lab, f"paper_state:{strategy_id}", db.json_dumps(strategy_state))
    finally:
        lab.close()


# ------------------------------------------------------------------ fills ----
def _bar(code: str, date: str) -> dict | None:
    mk = db.connect_market()
    try:
        r = db.q1(mk, "SELECT * FROM bars WHERE ts_code=? AND trade_date=?", (code, date))
        return dict(r) if r else None
    finally:
        mk.close()


def _fee(code: str, side: str, value: float, date: str) -> float:
    s = get_settings()
    fee = max(s.commission_min, s.commission_rate * value)
    if side == "SELL":
        fee += (0.001 if date < "20230828" else s.stamp_tax_sell) * value
    if code.endswith(".SH"):
        fee += s.transfer_fee * value
    return fee


def _fill_pending(strategy_id: str, date: str, state: dict) -> None:
    cfg = get_settings()
    remaining = []
    for o in sorted(state["pending"], key=lambda x: 0 if x["side"] == "SELL" else 1):
        if o["order_date"] != date:
            remaining.append(o)
            continue
        code, side = o["ts_code"], o["side"]
        bar = _bar(code, date)
        if bar is None or bar["is_suspended"]:
            o["note"] = f"deferred@{date}"
            remaining.append(o)
            _log(strategy_id, "WARN", f"order {o['order_id']} deferred (no bar/suspended) {code}")
            continue
        slip = cfg.slippage_bps / 10000.0
        pos = state["positions"].get(code)
        if side == "SELL":
            if not pos or pos["shares"] <= 0:
                _mark_order(o, "CANCELLED", "no position")
                continue
            op, dn = bar["open"], bar["down_limit"]
            if op is None or (dn is not None and op <= dn + 1e-9):
                remaining.append(o)
                continue
            price = max(op * (1 - slip), dn) if dn is not None else op * (1 - slip)
            shares = min(pos["available"], float(o["shares"]))
            if shares <= 0:
                remaining.append(o)
                continue
            v = shares * price
            fee = _fee(code, "SELL", v, date)
            state["cash"] += v - fee
            pos["shares"] -= shares
            pos["available"] -= shares
            pos["last_px"] = price
            _write_fill(o, date, code, "SELL", shares, price, fee, state["cash"])
            if pos["shares"] <= 1e-6:
                del state["positions"][code]
            _mark_order(o, "FILLED", "")
        else:
            op, up = bar["open"], bar["up_limit"]
            if op is None or up is None or op >= up - 1e-9:
                remaining.append(o)
                continue
            price = min(op * (1 + slip), up)
            shares = float(int(float(o["shares"]) // cfg.lot_size) * cfg.lot_size)
            while shares > 0:
                v = shares * price
                if v + _fee(code, "BUY", v, date) <= state["cash"]:
                    break
                shares -= cfg.lot_size
            if shares <= 0:
                _mark_order(o, "CANCELLED", "insufficient cash")
                continue
            v = shares * price
            fee = _fee(code, "BUY", v, date)
            state["cash"] -= v + fee
            pos = state["positions"].setdefault(code, {"shares": 0.0, "available": 0.0,
                                                       "avg_cost": 0.0, "last_px": price})
            pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + v + fee) / (pos["shares"] + shares)
            pos["shares"] += shares
            pos["last_px"] = price
            _write_fill(o, date, code, "BUY", shares, price, fee, state["cash"])
            _mark_order(o, "FILLED", "")
    state["pending"] = remaining


def _mark_order(o: dict, status: str, note: str) -> None:
    lab = db.connect_lab()
    try:
        lab.execute("UPDATE paper_orders SET status=?, note=? WHERE order_id=?",
                    (status, note, o["order_id"]))
    finally:
        lab.close()


def _write_fill(o: dict, date: str, code: str, side: str, shares: float, price: float,
                fee: float, cash_after: float) -> None:
    lab = db.connect_lab()
    try:
        db.upsert(lab, "paper_fills", [{
            "fill_id": uuid.uuid4().hex[:16], "order_id": o["order_id"],
            "strategy_id": o["strategy_id"], "trade_date": date, "ts_code": code,
            "side": side, "shares": shares, "price": price, "fee": fee,
            "cash_after": cash_after, "created_at": now_ts()}],
            ["fill_id", "order_id", "strategy_id", "trade_date", "ts_code", "side",
             "shares", "price", "fee", "cash_after", "created_at"])
    finally:
        lab.close()


# ------------------------------------------------------------------ signals ----
def _plan_orders(strategy_id: str, weights: dict[str, float], nav: float, date: str,
                 state: dict, bars: BarSource, cfg: EngineConfig) -> list[dict]:
    exec_date = cal.next_open(date)
    if exec_date is None:
        return []
    out = []
    for code, w in weights.items():
        w = float(min(max(w, 0.0), cfg.max_position_weight))
        bar = bars.bar(code, date)
        px = bar["close"] if bar else None
        if not px or px <= 0:
            continue
        pos = state["positions"].get(code)
        cur = pos["shares"] * px if pos else 0.0
        delta = w * nav - cur
        if abs(delta) / max(nav, 1.0) < cfg.rebalance_band:
            continue
        if delta < 0:
            shares = None if w == 0.0 else max(0.0, (pos["shares"] if pos else 0.0) - w * nav / px)
            side = "SELL"
        else:
            if delta < cfg.min_trade_value:
                continue
            tgt = float(int(w * nav / px // cfg.lot_size) * cfg.lot_size)
            shares = tgt - (pos["shares"] if pos else 0.0)
            if shares < cfg.lot_size:
                continue
            side = "BUY"
        out.append({"order_id": uuid.uuid4().hex[:16], "strategy_id": strategy_id,
                    "signal_date": date, "order_date": exec_date, "ts_code": code,
                    "side": side, "shares": shares if shares is not None else 0,
                    "status": "PENDING", "created_at": now_ts(),
                    "note": f"w={w:.3f}"})
    return out


def _signal_labels(weights: dict[str, float], state: dict) -> list[dict]:
    """Only these labels ever leave the paper system."""
    labels = []
    for code, w in weights.items():
        pos = state["positions"].get(code)
        cur_w = (pos["shares"] * (pos.get("last_px") or 0.0)) / 1e6 if pos else 0.0
        if w > 0 and (not pos or pos["shares"] <= 0):
            labels.append({"ts_code": code, "action": "BUY CANDIDATE", "target_weight": round(w, 4)})
        elif w == 0 and pos and pos["shares"] > 0:
            labels.append({"ts_code": code, "action": "SELL CANDIDATE", "target_weight": 0.0})
        else:
            labels.append({"ts_code": code, "action": "HOLD", "target_weight": round(w, 4)})
    if not labels:
        labels.append({"ts_code": None, "action": "NO TRADE", "target_weight": 0.0})
    return labels


# ------------------------------------------------------------------ main step ----
def paper_step(trade_date: str | None = None) -> dict:
    """Run the paper EOD step for the given (or latest open) session."""
    date = trade_date or cal.last_open()
    if not date or not cal.is_open(date):
        return {"skipped": "not an open session"}
    s = get_settings()
    cfg = EngineConfig.from_settings(s)
    bars: BarSource | None = None
    ran = []
    for sid in active_strategies():
        if sid not in STRATS:
            continue
        ensure_account(sid)
        state = _load_state(sid)
        # 1) fill orders planned for today
        _fill_pending(sid, date, state)
        # 2) mark NAV at close
        total = state["cash"]
        for code, st in state["positions"].items():
            b = _bar(code, date)
            if b and b["close"]:
                st["last_px"] = float(b["close"])
            total += st["shares"] * (st.get("last_px") or 0.0)
        bench_nav = None
        lab = db.connect_lab()
        try:
            db.upsert(lab, "paper_nav", [{
                "strategy_id": sid, "trade_date": date, "nav": total, "cash": state["cash"],
                "positions_value": total - state["cash"], "benchmark_nav": bench_nav}],
                ["strategy_id", "trade_date", "nav", "cash", "positions_value", "benchmark_nav"])
        finally:
            lab.close()
        # 3) strategy signals at close
        if bars is None:
            import datetime as dt
            warm_start = (dt.datetime.strptime(date, "%Y%m%d") - dt.timedelta(days=450)).strftime("%Y%m%d")
            bars = BarSource(start=warm_start, end=date, tier="production")
        pit = PITReader(date, strict=True)
        try:
            weights = STRATS[sid]().target_weights(
                _PaperContext(date=date, nav=total, pit=pit, bars=bars,
                              positions=_as_engine_positions(state), cfg=cfg,
                              engine=None, state=state["strategy_state"]),
                dict(STRATS[sid].default_params))
        except Exception as e:
            _log(sid, "ERROR", f"strategy failed: {e}")
            weights = {}
        finally:
            pit.close()
        labels = _signal_labels(weights, state)
        lab = db.connect_lab()
        try:
            db.upsert(lab, "paper_signals", [{
                "sig_id": uuid.uuid4().hex[:16], "strategy_id": sid, "signal_date": date,
                "payload_json": db.json_dumps(labels), "generated_at": now_ts(),
                "status": "EMITTED"}],
                ["sig_id", "strategy_id", "signal_date", "payload_json", "generated_at", "status"])
        finally:
            lab.close()
        orders = _plan_orders(sid, weights, total, date, state, bars, cfg)
        lab = db.connect_lab()
        try:
            if orders:
                db.upsert(lab, "paper_orders", orders,
                          ["order_id", "strategy_id", "signal_date", "order_date", "ts_code",
                           "side", "shares", "status", "created_at", "note"])
        finally:
            lab.close()
        if orders:
            state["pending"].extend(orders)
        _save_state(sid, state["cash"], state["positions"], state["strategy_state"])
        ran.append({"strategy_id": sid, "nav": total, "signals": len(labels),
                    "orders": len(orders)})
    return {"trade_date": date, "strategies": ran}


def _as_engine_positions(state: dict) -> dict:
    out = {}
    for code, st in state["positions"].items():
        from ..backtest.engine import Position
        out[code] = Position(ts_code=code, shares=st["shares"], available=st["available"],
                             avg_cost=st["avg_cost"], last_price=st.get("last_px") or 0.0)
    return out


class _PaperContext:
    """Duck-typed StrategyContext for paper signal generation."""
    def __init__(self, date, nav, pit, bars, positions, cfg, engine, state):
        self.date, self.nav, self.pit, self.bars = date, nav, pit, bars
        self.positions, self.cfg, self.engine, self.state = positions, cfg, engine, state


# ------------------------------------------------------------------ QA ----
def ledger_consistency(strategy_id: str | None = None) -> dict:
    """PAPER ledger must reconcile: cash = initial + flows; nav = cash + holdings."""
    lab = db.connect_lab()
    mk = db.connect_market()
    try:
        sids = [strategy_id] if strategy_id else \
            [r["strategy_id"] for r in db.q(lab, "SELECT strategy_id FROM paper_account")]
        report = []
        for sid in sids:
            acc = db.q1(lab, "SELECT * FROM paper_account WHERE strategy_id=?", (sid,))
            if not acc:
                continue
            fills = db.q(lab, "SELECT * FROM paper_fills WHERE strategy_id=? ORDER BY trade_date, rowid", (sid,))
            cash = acc["initial_cash"]
            problems = []
            for f in fills:
                v = f["shares"] * f["price"]
                cash += (-v - f["fee"]) if f["side"] == "BUY" else (v - f["fee"])
                if abs(cash - f["cash_after"]) > 0.01:
                    problems.append(f"cash mismatch at fill {f['fill_id']}")
            pos_rows = db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (sid,))
            holdings = 0.0
            if abs(cash - acc["cash"]) > 0.01:
                problems.append("account cash != initial + fills (tampered or corrupted)")
            last_nav = db.q1(lab, "SELECT trade_date, nav FROM paper_nav WHERE strategy_id=? "
                                  "ORDER BY trade_date DESC LIMIT 1", (sid,))
            nav_ok = True
            if last_nav:
                mark_date = last_nav["trade_date"]
                for p in pos_rows:
                    r = db.q1(mk, "SELECT close FROM bars WHERE ts_code=? AND trade_date<=? "
                                  "ORDER BY trade_date DESC LIMIT 1", (p["ts_code"], mark_date))
                    px = r["close"] if r and r["close"] else p["last_price"]
                    holdings += (p["shares"] or 0) * (px or 0)
                if abs((cash + holdings) - last_nav["nav"]) > max(1.0, 1e-4 * abs(last_nav["nav"])):
                    nav_ok = False
                    problems.append("NAV != cash + holdings (last mark)")
            else:
                for p in pos_rows:
                    r = db.q1(mk, "SELECT close FROM bars WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                              (p["ts_code"],))
                    px = r["close"] if r and r["close"] else p["last_price"]
                    holdings += (p["shares"] or 0) * (px or 0)
            report.append({"strategy_id": sid, "ok": not problems,
                           "cash_recomputed": cash, "holdings": holdings,
                           "problems": problems})
        return {"ok": all(r["ok"] for r in report), "accounts": report}
    finally:
        lab.close()
        mk.close()
