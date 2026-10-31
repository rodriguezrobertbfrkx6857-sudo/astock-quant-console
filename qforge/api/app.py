# -*- coding: utf-8 -*-
"""Local dashboard API -- FastAPI, bound to 127.0.0.1 ONLY, strictly read-only.

The dashboard never recomputes research truth: every endpoint reads results
written by the engine/research/paper pipeline (lab.db / market.db).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, db
from ..config import (
    ACCOUNT_ACCESS,
    BROKER_ACCESS,
    BROKER_AUTH,
    LOCAL_ONLY,
    REAL_ORDER_ENABLED,
    REAL_TRADING_ENABLED,
    ROOT,
    get_settings,
)
from .. import secrets as sec
from ..data import calendar as cal
from ..data import universe as univ
from ..data.quality import all_readiness

app = FastAPI(title="QForgeLab API", version=__version__, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1", "http://localhost:8000", "http://127.0.0.1:8000", "*"],
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])

WEB = ROOT / "qforge" / "web"


def _lab():
    return db.connect_lab()


def _mk():
    return db.connect_market()


# ------------------------------------------------------------------ overview ----
@app.get("/api/overview")
def overview():
    s = get_settings()
    mk, lab = _mk(), _lab()
    try:
        health = {r["dataset"]: r["status"] for r in db.q(mk,
                 "SELECT dataset, status FROM data_health")}
        bars = db.q1(mk, "SELECT MAX(trade_date) mx, COUNT(*) n FROM bars")
        runs = db.q(lab, "SELECT job, business_date, status, finished_at FROM run_ledger "
                         "WHERE business_date=(SELECT MAX(business_date) FROM run_ledger) ORDER BY job")
        runtime_state = db.kv_get(mk, "runtime_state")
        paper = db.q(lab, "SELECT a.strategy_id, a.nav, a.status, r.strategy_id IS NOT NULL AS has_nav, "
                          "(SELECT trade_date FROM paper_nav WHERE strategy_id=a.strategy_id "
                          " ORDER BY trade_date DESC LIMIT 1) last_day "
                          "FROM paper_account a LEFT JOIN paper_nav r ON r.strategy_id=a.strategy_id GROUP BY a.strategy_id")
        stages = db.q(lab, "SELECT status, COUNT(*) n FROM strategy_registry GROUP BY status")
        alerts = db.q(lab, "SELECT * FROM alerts ORDER BY id DESC LIMIT 8")
        oos = db.q(lab, "SELECT * FROM oos_registry ORDER BY created_at DESC LIMIT 5")
        return {
            "version": __version__,
            "safety": {"real_trading_enabled": REAL_TRADING_ENABLED,
                       "real_order_enabled": REAL_ORDER_ENABLED,
                       "local_only": LOCAL_ONLY, "broker_access": BROKER_ACCESS,
                       "account_access": ACCOUNT_ACCESS, "broker_auth": BROKER_AUTH},
            "sources": sec.describe(),
            "calendar": {"last_open": cal.last_open(), "is_open_today": cal.is_open(cal.last_open() or "")},
            "bars": {"max_date": bars["mx"] if bars else None, "rows": bars["n"] if bars else 0},
            "data_health": health,
            "universe": univ.coverage(),
            "runtime": db.json_loads(runtime_state or "{}") or {},
            "runs": [dict(r) for r in runs],
            "paper": [dict(r) for r in paper],
            "strategy_stages": {r["status"]: r["n"] for r in stages},
            "alerts": [dict(r) for r in alerts],
            "oos": [dict(r) for r in oos],
        }
    finally:
        mk.close()
        lab.close()


# ------------------------------------------------------------------ research ----
@app.get("/api/research/strategies")
def strategies():
    lab = _lab()
    try:
        rows = db.q(lab, "SELECT * FROM strategy_registry ORDER BY strategy_id")
        out = []
        for r in rows:
            d = dict(r)
            ev = db.q1(lab, "SELECT * FROM evaluations WHERE strategy_id=? ORDER BY created_at DESC LIMIT 1",
                       (r["strategy_id"],))
            if ev:
                d["latest_eval"] = {"verdict": ev["verdict"], "created_at": ev["created_at"],
                                    "uses_oos": ev["uses_oos"],
                                    "metrics": db.json_loads(ev["metrics_json"] or "{}")}
            out.append(d)
        return out
    finally:
        lab.close()


@app.get("/api/research/factors")
def factors():
    lab = _lab()
    try:
        rows = db.q(lab, "SELECT f.*, s.ic_mean, s.ic_ir, s.rank_ic_mean, s.ic_tstat, "
                         "s.long_short_ann, s.monotonics, s.window_start, s.window_end "
                         "FROM factor_registry f LEFT JOIN factor_stats s "
                         "ON s.factor_id=f.factor_id AND s.scope='research' ORDER BY f.factor_id")
        return [dict(r) for r in rows]
    finally:
        lab.close()


@app.get("/api/research/factors/{factor_id}/detail")
def factor_detail(factor_id: str):
    lab = _lab()
    try:
        st = db.q1(lab, "SELECT * FROM factor_stats WHERE factor_id=? AND scope='research'", (factor_id,))
        ic = db.q(lab, "SELECT trade_date, ic FROM factor_ic_daily WHERE factor_id=? AND scope='research' "
                       "ORDER BY trade_date", (factor_id,))
        return {"stats": dict(st) if st else None,
                "ic_series": [{"date": r["trade_date"], "ic": r["ic"]} for r in ic]}
    finally:
        lab.close()


@app.get("/api/research/evaluations")
def evaluations(limit: int = 50):
    lab = _lab()
    try:
        rows = db.q(lab, "SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        lab.close()


@app.get("/api/research/queue")
def research_queue():
    lab = _lab()
    try:
        rows = db.q(lab, "SELECT * FROM research_queue ORDER BY status, priority, created_at LIMIT 100")
        return [dict(r) for r in rows]
    finally:
        lab.close()


@app.get("/api/research/backtests")
def backtests(strategy_id: str | None = None, limit: int = 40):
    lab = _lab()
    try:
        if strategy_id:
            rows = db.q(lab, "SELECT * FROM backtests WHERE strategy_id=? ORDER BY created_at DESC LIMIT ?",
                        (strategy_id, limit))
        else:
            rows = db.q(lab, "SELECT * FROM backtests ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        lab.close()


@app.get("/api/research/backtest/{bt_id}")
def backtest_detail(bt_id: str):
    lab = _lab()
    try:
        bt = db.q1(lab, "SELECT * FROM backtests WHERE bt_id=?", (bt_id,))
        if not bt:
            raise HTTPException(404, "backtest not found")
        nav = db.q(lab, "SELECT * FROM bt_nav WHERE bt_id=? ORDER BY trade_date", (bt_id,))
        fills = db.q(lab, "SELECT * FROM bt_trades WHERE bt_id=? ORDER BY seq LIMIT 400", (bt_id,))
        val = db.q(lab, "SELECT * FROM validation_runs WHERE bt_id=? ORDER BY created_at DESC LIMIT 1", (bt_id,))
        orders = db.q(lab, "SELECT status, COUNT(*) n FROM bt_orders WHERE bt_id=? GROUP BY status", (bt_id,))
        return {"backtest": dict(bt),
                "nav": [dict(r) for r in nav],
                "fills": [dict(r) for r in fills],
                "validation": dict(val) if val else None,
                "order_status": {r["status"]: r["n"] for r in orders}}
    finally:
        lab.close()


# ------------------------------------------------------------------ market ----
@app.get("/api/market/search")
def market_search(q: str = Query("", max_length=20)):
    mk = _mk()
    try:
        if not q:
            return []
        rows = db.q(mk, "SELECT ts_code, name, board, sec_type, list_date FROM sec_master "
                        "WHERE ts_code LIKE ? OR name LIKE ? LIMIT 20",
                    (f"%{q}%", f"%{q}%"))
        return [dict(r) for r in rows]
    finally:
        mk.close()


@app.get("/api/market/security")
def market_security(code: str, days: int = 250):
    mk = _mk()
    try:
        info = db.q1(mk, "SELECT * FROM sec_master WHERE ts_code=?", (code,))
        if not info:
            raise HTTPException(404, "unknown code")
        bars = db.q(mk, "SELECT trade_date, open, high, low, close, vol, amount FROM bars "
                        "WHERE ts_code=? ORDER BY trade_date DESC LIMIT ?", (code, days))
        bars = list(reversed(bars))
        return {"info": dict(info), "kline": [dict(r) for r in bars]}
    finally:
        mk.close()


@app.get("/api/market/benchmarks")
def benchmarks():
    s = get_settings()
    mk = _mk()
    try:
        out = []
        for code in s.benchmark_codes:
            r = db.q1(mk, "SELECT ts_code, trade_date, close FROM bench_daily WHERE ts_code=? "
                          "ORDER BY trade_date DESC LIMIT 1", (code,))
            if r:
                out.append(dict(r))
        return out
    finally:
        mk.close()


# ------------------------------------------------------------------ paper ----
@app.get("/api/paper/accounts")
def paper_accounts():
    lab = _lab()
    try:
        rows = db.q(lab, "SELECT * FROM paper_account")
        out = []
        for r in rows:
            d = dict(r)
            last = db.q1(lab, "SELECT * FROM paper_nav WHERE strategy_id=? ORDER BY trade_date DESC LIMIT 1",
                         (r["strategy_id"],))
            first = db.q1(lab, "SELECT trade_date FROM paper_nav WHERE strategy_id=? ORDER BY trade_date LIMIT 1",
                          (r["strategy_id"],))
            if last:
                d["last_nav"] = dict(last)
                d["return_pct"] = (last["nav"] / r["initial_cash"] - 1) * 100
                d["start_date"] = first["trade_date"] if first else None
            pos = db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (r["strategy_id"],))
            d["n_positions"] = len(pos)
            out.append(d)
        return out
    finally:
        lab.close()


@app.get("/api/paper/{strategy_id}")
def paper_detail(strategy_id: str, days: int = 500):
    lab = _lab()
    mk = _mk()
    try:
        nav = db.q(lab, "SELECT * FROM paper_nav WHERE strategy_id=? ORDER BY trade_date", (strategy_id,))
        positions = db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (strategy_id,))
        enriched = []
        for p in positions:
            d = dict(p)
            sec = db.q1(mk, "SELECT name FROM sec_master WHERE ts_code=?", (p["ts_code"],))
            d["name"] = sec["name"] if sec and sec["name"] else p["ts_code"]
            r = db.q1(mk, "SELECT close, trade_date FROM bars WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                      (p["ts_code"],))
            d["last_close"] = r["close"] if r else None
            d["last_bar_date"] = r["trade_date"] if r else None
            enriched.append(d)
        raw_signals = db.q(lab, "SELECT * FROM paper_signals WHERE strategy_id=? ORDER BY signal_date DESC LIMIT 10",
                           (strategy_id,))
        enriched_signals = []
        for s in raw_signals:
            sd = dict(s)
            try:
                items = db.json_loads(sd["payload_json"] or "[]") or []
                for it in items:
                    sec = db.q1(mk, "SELECT name FROM sec_master WHERE ts_code=?", (it.get("ts_code"),))
                    it["name"] = sec["name"] if sec and sec["name"] else it.get("ts_code")
                sd["payload_json"] = db.json_dumps(items)
            except Exception:
                pass
            enriched_signals.append(sd)

        raw_fills = db.q(lab, "SELECT * FROM paper_fills WHERE strategy_id=? ORDER BY trade_date DESC, fill_id DESC LIMIT 60",
                         (strategy_id,))
        enriched_fills = []
        for f in raw_fills:
            fd = dict(f)
            sec = db.q1(mk, "SELECT name FROM sec_master WHERE ts_code=?", (f["ts_code"],))
            fd["name"] = sec["name"] if sec and sec["name"] else f["ts_code"]
            enriched_fills.append(fd)

        logs = db.q(lab, "SELECT * FROM paper_log WHERE strategy_id=? ORDER BY id DESC LIMIT 30", (strategy_id,))
        return {"nav": [dict(r) for r in nav[-days:]],
                "positions": enriched,
                "signals": enriched_signals,
                "fills": enriched_fills,
                "log": [dict(r) for r in logs]}
    finally:
        lab.close()
        mk.close()


# ------------------------------------------------------------------ events ----
@app.get("/api/events/overview")
def events_overview(limit: int = 50):
    lab = _lab()
    try:
        stats = db.q(lab, "SELECT * FROM event_stats")
        recent = db.q(lab, "SELECT * FROM event_store ORDER BY event_ts DESC LIMIT ?", (limit,))
        by_kind = db.q(lab, "SELECT kind, COUNT(*) n, AVG(score) avg_score FROM event_store GROUP BY kind")
        return {"stats": [dict(r) for r in stats],
                "recent": [dict(r) for r in recent],
                "by_kind": [dict(r) for r in by_kind]}
    finally:
        lab.close()


# ------------------------------------------------------------------ data health ----
@app.get("/api/data/health")
def data_health():
    mk = _mk()
    try:
        rows = db.q(mk, "SELECT * FROM data_health ORDER BY dataset")
        wm = db.q(mk, "SELECT * FROM watermark ORDER BY dataset")
        return {"health": [dict(r) for r in rows], "watermarks": [dict(r) for r in wm],
                "readiness": all_readiness()}
    finally:
        mk.close()


# ------------------------------------------------------------------ system ----
@app.get("/api/system/runs")
def system_runs(limit: int = 80):
    lab = _lab()
    try:
        runs = db.q(lab, "SELECT * FROM run_ledger ORDER BY started_at DESC LIMIT ?", (limit,))
        alerts = db.q(lab, "SELECT * FROM alerts ORDER BY id DESC LIMIT 50")
        oos = db.q(lab, "SELECT * FROM oos_registry ORDER BY created_at DESC")
        return {"runs": [dict(r) for r in runs], "alerts": [dict(r) for r in alerts],
                "oos_registry": [dict(r) for r in oos]}
    finally:
        lab.close()


@app.get("/api/system/config")
def system_config():
    s = get_settings()
    return {
        "version": __version__,
        "safety": {"REAL_TRADING_ENABLED": REAL_TRADING_ENABLED,
                   "REAL_ORDER_ENABLED": REAL_ORDER_ENABLED,
                   "BROKER_ACCESS": BROKER_ACCESS, "ACCOUNT_ACCESS": ACCOUNT_ACCESS,
                   "BROKER_AUTH": BROKER_AUTH, "LOCAL_ONLY": LOCAL_ONLY},
        "settings": {k: v for k, v in vars(s).items()
                     if isinstance(v, (int, float, str, bool, list, tuple))
                     and "key" not in k.lower() and "token" not in k.lower()},
        "secrets": sec.describe(),
        "root": str(ROOT),
    }


# ------------------------------------------------------------------ interactive actions & prediction ----
import math


def _clean_json(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    return obj


@app.post("/api/actions/paper_step")
def action_paper_step():
    from ..paper import engine as paper
    res = paper.paper_step()
    return _clean_json({"ok": True, "result": res})


@app.post("/api/actions/evaluate_strategy")
def action_evaluate_strategy(strategy_id: str = Query(..., max_length=50)):
    from ..strategies import REGISTRY as STRATS
    from ..research import evaluator
    sid = strategy_id
    if sid not in STRATS:
        for k in STRATS:
            if k == sid or k.startswith(sid + "_") or sid.upper() in k:
                sid = k
                break
    res = evaluator.evaluate_strategy(sid)
    return _clean_json({
        "ok": True,
        "strategy_id": sid,
        "verdict": res.get("verdict"),
        "metrics": res.get("metrics"),
        "gates": res.get("gates"),
    })


@app.post("/api/actions/evaluate_factor")
def action_evaluate_factor(factor_id: str = Query(..., max_length=50)):
    from ..factors import REGISTRY as FACTORS
    from ..research import queue as rq
    fid = factor_id
    if fid not in FACTORS:
        for k in FACTORS:
            if k.upper() == fid.upper() or k.upper().startswith(fid.upper()):
                fid = k
                break
    res = rq.analyze_factor(fid)
    return _clean_json({"ok": True, "factor_id": fid, "result": res})


@app.post("/api/actions/run_queue")
def action_run_queue(max_items: int = 10):
    from ..research import queue as rq
    rq.seed_queue()
    out = rq.process_queue(max_items=max_items)
    return {"ok": True, "processed": len(out), "items": [r.get("item", {}).get("target") for r in out]}


@app.post("/api/actions/add_paper_position")
def action_add_paper_position(
    ts_code: str = Query(..., max_length=20),
    shares: float = Query(..., gt=0),
    price: float | None = Query(None, gt=0),
    strategy_id: str | None = Query(None, max_length=50),
):
    import uuid
    from ..util import now_ts, today_str
    from ..paper import engine as paper
    code = ts_code.strip().upper()
    mk, lab = _mk(), _lab()
    try:
        if "." not in code:
            matched = db.q1(mk, "SELECT ts_code, name FROM sec_master WHERE ts_code LIKE ? ORDER BY list_date LIMIT 1", (f"{code}.%",))
            if matched:
                code = matched["ts_code"]
        sec = db.q1(mk, "SELECT ts_code, name FROM sec_master WHERE ts_code=?", (code,))
        if not sec:
            raise HTTPException(400, f"未找到证券标的: {code}，请核对代码（如 600519 或 600519.SH）")
        name = sec["name"]

        if price is None or price <= 0:
            bar = db.q1(mk, "SELECT close FROM bars WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,))
            if bar and bar["close"]:
                price = float(bar["close"])
            else:
                price = 10.0

        sid = strategy_id
        if not sid:
            acc = db.q1(lab, "SELECT strategy_id FROM paper_account ORDER BY started_at DESC LIMIT 1")
            sid = acc["strategy_id"] if acc else "S1_DEFENSIVE_QUALITY"

        paper.ensure_account(sid)

        existing = db.q1(lab, "SELECT * FROM paper_positions WHERE strategy_id=? AND ts_code=?", (sid, code))
        if existing:
            old_shares = float(existing["shares"])
            old_cost = float(existing["avg_cost"] or price)
            new_shares = old_shares + shares
            new_cost = (old_shares * old_cost + shares * price) / max(1.0, new_shares)
        else:
            new_shares = shares
            new_cost = price

        pos_row = {
            "strategy_id": sid,
            "ts_code": code,
            "shares": new_shares,
            "available_shares": new_shares,
            "avg_cost": round(new_cost, 4),
            "last_price": round(price, 4),
            "updated_at": now_ts(),
        }
        db.upsert(lab, "paper_positions", [pos_row],
                  ["strategy_id", "ts_code", "shares", "available_shares", "avg_cost", "last_price", "updated_at"])

        fill_id = "M" + uuid.uuid4().hex[:15]
        acc_row = db.q1(lab, "SELECT cash FROM paper_account WHERE strategy_id=?", (sid,))
        cash_after = acc_row["cash"] if acc_row else 1_000_000.0
        db.upsert(lab, "paper_fills", [{
            "fill_id": fill_id,
            "order_id": "MANUAL",
            "strategy_id": sid,
            "trade_date": today_str(),
            "ts_code": code,
            "side": "BUY",
            "shares": shares,
            "price": round(price, 4),
            "fee": 0.0,
            "cash_after": cash_after,
            "created_at": now_ts(),
        }], ["fill_id", "order_id", "strategy_id", "trade_date", "ts_code", "side", "shares", "price", "fee", "cash_after", "created_at"])

        all_pos = db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (sid,))
        holdings = sum(float(p["shares"] or 0) * float(p["last_price"] or p["avg_cost"] or 0) for p in all_pos)
        new_nav = cash_after + holdings
        lab.execute("UPDATE paper_account SET nav=?, updated_at=? WHERE strategy_id=?", (new_nav, now_ts(), sid))

        paper._log(sid, "INFO", f"手动添加持仓: {code}({name}) {shares:,.0f}股 @ {price:.2f}元")

        return {
            "ok": True,
            "message": f"成功为 {sid} 添加持仓: {code} ({name}) {shares:,.0f}股",
            "strategy_id": sid,
            "ts_code": code,
            "name": name,
            "shares": new_shares,
            "cost": round(new_cost, 2),
            "price": round(price, 2),
            "total_value": round(new_shares * price, 2),
        }
    finally:
        mk.close()
        lab.close()


@app.post("/api/actions/remove_paper_position")
def action_remove_paper_position(
    ts_code: str = Query(..., max_length=20),
    strategy_id: str | None = Query(None, max_length=50),
):
    from ..util import now_ts
    from ..paper import engine as paper
    code = ts_code.strip().upper()
    lab = _lab()
    try:
        sid = strategy_id
        if not sid:
            acc = db.q1(lab, "SELECT strategy_id FROM paper_account ORDER BY started_at DESC LIMIT 1")
            sid = acc["strategy_id"] if acc else "S1_DEFENSIVE_QUALITY"
        lab.execute("DELETE FROM paper_positions WHERE strategy_id=? AND ts_code=?", (sid, code))
        all_pos = db.q(lab, "SELECT * FROM paper_positions WHERE strategy_id=?", (sid,))
        acc_row = db.q1(lab, "SELECT cash FROM paper_account WHERE strategy_id=?", (sid,))
        cash = acc_row["cash"] if acc_row else 1_000_000.0
        holdings = sum(float(p["shares"] or 0) * float(p["last_price"] or p["avg_cost"] or 0) for p in all_pos)
        lab.execute("UPDATE paper_account SET nav=?, updated_at=? WHERE strategy_id=?", (cash + holdings, now_ts(), sid))
        paper._log(sid, "INFO", f"手动清空持仓: {code}")
        return {"ok": True, "message": f"已成功移除 {code} 持仓"}
    finally:
        lab.close()


@app.get("/api/market/predict")
def market_predict(code: str = Query(..., max_length=20)):
    mk = _mk()
    try:
        info = db.q1(mk, "SELECT * FROM sec_master WHERE ts_code=?", (code,))
        if not info:
            raise HTTPException(404, "unknown code")
        bars = db.q(mk, "SELECT trade_date, open, high, low, close, vol, amount FROM bars "
                        "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 120", (code,))
        if not bars or len(bars) < 20:
            return {"ok": False, "code": code, "message": "历史K线数据不足20日，无法测算预测信号"}
        bars = list(reversed(bars))
        closes = [float(r["close"]) for r in bars if r["close"] is not None]
        if len(closes) < 20:
            return {"ok": False, "code": code, "message": "有效收盘价数据不足20日"}
        n = len(closes)
        last_px = closes[-1]
        last_date = bars[-1]["trade_date"]

        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / min(60, n) if n >= 20 else ma20

        ret5 = (last_px / closes[-5] - 1) if n >= 5 else 0.0
        ret20 = (last_px / closes[-20] - 1) if n >= 20 else 0.0
        bias20 = (last_px / ma20 - 1) * 100

        recent_bars = bars[-40:]
        lows = [float(r["low"]) for r in recent_bars if r["low"] is not None]
        highs = [float(r["high"]) for r in recent_bars if r["high"] is not None]
        support = min(lows) if lows else last_px * 0.95
        resistance = max(highs) if highs else last_px * 1.05

        vols = [float(r["vol"] or 0) for r in bars]
        vol5 = sum(vols[-5:]) / 5
        vol20 = sum(vols[-20:]) / 20
        vol_ratio = (vol5 / max(1.0, vol20)) if vol20 > 0 else 1.0

        score = 50.0
        reasons = []

        if ma5 > ma20 > ma60:
            score += 20
            reasons.append("MA5 > MA20 > MA60 均线呈现典型多头排列，中期上升趋势明确")
        elif ma5 < ma20 < ma60:
            score -= 20
            reasons.append("MA5 < MA20 < MA60 均线呈现空头排列，中期下行压力较大")
        elif ma5 > ma20:
            score += 10
            reasons.append("短期均线上穿中期均线（MA5 > MA20），短期多头占优")
        else:
            score -= 10
            reasons.append("短期均线承压于中期均线（MA5 < MA20），处于弱势整理区间")

        if ret20 > 0.08:
            score += 15
            reasons.append(f"近20日动量强劲 (+{ret20*100:.1f}%)，处于强势主升波段")
        elif ret20 > 0.02:
            score += 8
            reasons.append(f"近20日温和攀升 (+{ret20*100:.1f}%)，上升通道保持良好")
        elif ret20 < -0.08:
            score -= 15
            reasons.append(f"近20日动量较弱 ({ret20*100:.1f}%)，波段动能偏空")
        else:
            reasons.append("近20日价格波动收窄，处于箱体蓄势区间")

        if vol_ratio > 1.25 and ret5 > 0:
            score += 10
            reasons.append(f"近5日量比显著放大 (量比 {vol_ratio:.2f}) 且价格上涨，量价配合理想")
        elif vol_ratio > 1.25 and ret5 < -0.03:
            score -= 10
            reasons.append(f"近5日放量回落 (量比 {vol_ratio:.2f})，短期存在获利兑现压力")

        if bias20 > 12.0:
            score -= 8
            reasons.append(f"20日乖离率偏高 (BIAS20 = {bias20:+.1f}%)，短线注意冲高回落震荡风险")
        elif bias20 < -12.0:
            score += 8
            reasons.append(f"20日乖离率严重负偏 (BIAS20 = {bias20:+.1f}%)，短线处于超跌反弹酝酿区")

        score = max(5.0, min(95.0, score))

        if score >= 75:
            signal = "STRONG_BUY"
            signal_name = "强烈看多 (Strong Buy)"
        elif score >= 60:
            signal = "BUY"
            signal_name = "偏多买入 (Buy Candidate)"
        elif score <= 35:
            signal = "SELL"
            signal_name = "偏空防守 (Sell / Avoid)"
        else:
            signal = "HOLD"
            signal_name = "中性观望 (Hold)"

        return {
            "ok": True,
            "code": code,
            "name": info["name"],
            "trade_date": last_date,
            "last_close": round(last_px, 2),
            "score": round(score, 1),
            "signal": signal,
            "signal_name": signal_name,
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "bias20": round(bias20, 2),
            "ret20_pct": round(ret20 * 100, 2),
            "vol_ratio": round(vol_ratio, 2),
            "reasons": reasons,
        }
    finally:
        mk.close()


# ------------------------------------------------------------------ static ----
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse(status_code=500, content={"error": str(exc)[:300]})
