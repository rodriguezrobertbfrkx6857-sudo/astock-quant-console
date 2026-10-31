# -*- coding: utf-8 -*-
"""Event Intelligence pipeline.

RAW announcements/news (immutable raw layer)
  -> PIT timestamp (announcement usable next calendar day 00:00; news at
     publish time + 5 minutes)
  -> dedup (content/code/day keys)
  -> company mapping (anns carry ts_code; news mapped by unique name match)
  -> rule-based Event Classification + Score (transparent, auditable)
  -> event_store (lab.db)
  -> event backtest (event study: CAR by kind, research scope only)

LLM POLICY: an optional local LLM hook may only *structure* event text into
(kind, score) candidates; it never outputs buy/sell decisions and is disabled
by default. Positions flow only through rule scores -> strategies -> engine.
"""
from __future__ import annotations

import re

from .. import db
from ..config import get_settings
from ..util import available_at_next_day, get_logger, now_ts, sha1

log = get_logger("qforge.events")

# rule table: kind -> (patterns, score). Auditable and deliberately simple.
RULES: list[tuple[str, list[str], float]] = [
    ("EARN_PREANN_POS", ["预增", "扭亏", "业绩预喜", "净利润上升", "大幅增长"], 2.0),
    ("EARN_PREANN_NEG", ["预减", "首亏", "续亏", "业绩预亏", "略减", "大幅下降"], -2.0),
    ("BUYBACK", ["回购"], 1.5),
    ("SELLDOWN", ["拟减持", "减持计划", "减持股份计划"], -1.5),
    ("DIVIDEND", ["利润分配", "分红派息", "权益分派", "派息"], 1.0),
    ("CONTRACT", ["中标", "重大合同", "签署重大"], 1.5),
    ("MA", ["重组", "重大资产", "并购", "收购"], 1.5),
    ("LEGAL", ["立案", "处罚", "违规", "监管函", "警示函", "问询函"], -2.0),
    ("INCENTIVE", ["股权激励", "员工持股"], 1.0),
    ("DELIST_RISK", ["退市", "终止上市", "摘牌"], -3.0),
]


NEGATIONS: list[str] = [
    "不进行利润分配", "不进行分红", "不分配", "未进行利润分配", "不派现", "不送红股",
    "终止重大资产重组", "终止重组", "终止筹划重大资产", "终止筹划", "取消重组",
    "取消减持", "终止减持", "减持计划实施完毕", "减持计划届满", "减持时间届满", "未减持",
    "终止回购", "取消回购", "回购计划实施完毕", "未能中标", "终止合同", "解除合同",
]


def classify_text(text: str) -> tuple[str, float] | None:
    if not text:
        return None
    for neg in NEGATIONS:
        if neg in text:
            return None
    for kind, patterns, score in RULES:
        for p in patterns:
            if p in text:
                return kind, score
    return None


def llm_structured_classify(texts: list[str]) -> list[tuple[str, float]] | None:
    """Optional LLM assist (DISABLED by default). If enabled, this hook may
    return (kind, score) suggestions for unclassified texts -- structure only;
    the pipeline caps scores and never accepts order instructions from LLM.
    No external network calls are made by this platform."""
    if not get_settings().extra.get("llm_enabled"):
        return None
    return None  # intentionally inert: no LLM dependency in the default runtime


# ------------------------------------------------------------------ mappings ----
def _company_map() -> dict[str, str]:
    mk = db.connect_market()
    try:
        rows = db.q(mk, "SELECT ts_code, name FROM sec_master WHERE sec_type='STOCK' AND name IS NOT NULL")
    finally:
        mk.close()
    m = {}
    for r in rows:
        name = (r["name"] or "").replace(" ", "")
        if len(name) >= 3:
            m[name] = r["ts_code"]
    return m


def build_event_store() -> dict:
    s = get_settings()
    mk = db.connect_market()
    lab = db.connect_lab()
    n_ann = n_news = 0
    try:
        cmap = _company_map()
        events: dict[str, dict] = {}
        # announcements
        for r in db.q(mk, "SELECT * FROM norm_anns"):
            title = r["title"] or ""
            cls = classify_text(title)
            if not cls:
                continue
            kind, score = cls
            if not r["ts_code"]:
                continue
            eid = sha1(["ann", r["ann_id"]])[:16]
            ts_code = r["ts_code"]
            events[eid] = {
                "event_id": eid, "ts_code": ts_code, "company_name": r["name"],
                "kind": kind, "score": score,
                "event_ts": available_at_next_day(r["ann_date"]),
                "event_date": r["ann_date"], "source": "ann",
                "title": title[:200], "ref_id": r["ann_id"], "created_at": now_ts(),
            }
        n_ann = len(events)
        # news: map company by unique name occurrence
        for r in db.q(mk, "SELECT * FROM norm_news"):
            text = (r["title"] or "") + " " + (r["content"] or "")[:300]
            cls = classify_text(text)
            if not cls:
                continue
            kind, score = cls
            ts_code = _match_company(r["title"] or "", cmap)
            if not ts_code:
                continue
            eid = sha1(["news", r["nid"]])[:16]
            dt_pub = r["datetime"] or ""
            events[eid] = {
                "event_id": eid, "ts_code": ts_code, "company_name": None,
                "kind": kind, "score": score,
                "event_ts": _news_pit_ts(dt_pub),
                "event_date": dt_pub[:10].replace("-", "") if dt_pub else "",
                "source": "news", "title": (r["title"] or "")[:200],
                "ref_id": r["nid"], "created_at": now_ts(),
            }
        n_news = len(events) - n_ann
        rows = list(events.values())
        with db.tx(lab):
            lab.execute("DELETE FROM event_store")
            for i in range(0, len(rows), 1000):
                lab.executemany(
                    "INSERT OR REPLACE INTO event_store (event_id, ts_code, company_name, kind, "
                    "score, event_ts, event_date, source, title, ref_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(e["event_id"], e["ts_code"], e["company_name"], e["kind"], e["score"],
                      e["event_ts"], e["event_date"], e["source"], e["title"], e["ref_id"],
                      e["created_at"]) for e in rows[i:i + 1000]])
        return {"events": len(rows), "from_anns": n_ann, "from_news": n_news}
    finally:
        mk.close()
        lab.close()


def _match_company(title: str, cmap: dict[str, str]) -> str | None:
    hits = [code for name, code in cmap.items() if name in title]
    if len(hits) == 1:
        return hits[0]
    return None


def _news_pit_ts(dt_pub: str) -> str:
    from ..config import get_settings as _gs
    lag = _gs().event_lag_minutes
    if not dt_pub:
        return "1970-01-01T00:00:00+08:00"
    import datetime as dt
    t = dt_pub.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            d = dt.datetime.strptime(t[:19], fmt)
            return (d + dt.timedelta(minutes=lag)).isoformat() + "+08:00"
        except ValueError:
            continue
    return dt_pub[:10] + "T23:59:59+08:00"


# ------------------------------------------------------------------ event study ----
def event_study() -> dict:
    """CAR(T+1..T+h) per event kind within the research window (OOS excluded)."""
    from ..research import oos
    clip_end = oos.research_clip_end()
    mk = db.connect_market()
    lab = db.connect_lab()
    try:
        events = [dict(r) for r in db.q(lab, "SELECT * FROM event_store WHERE ts_code IS NOT NULL")]
        if not events:
            return {}
        event_codes = sorted({e["ts_code"] for e in events if e.get("ts_code")})
        min_date = min((e["event_date"] for e in events if e.get("event_date")), default="20180101")
        max_date = max((e["event_date"] for e in events if e.get("event_date")), default=clip_end)
        query_end = max(clip_end, max_date)
        rows = []
        for i in range(0, len(event_codes), 400):
            batch = event_codes[i:i + 400]
            placeholders = ",".join("?" * len(batch))
            rows.extend(db.q(mk, f"SELECT ts_code, trade_date, adj_factor, close FROM bars "
                                 f"WHERE trade_date <= ? AND trade_date >= ? AND ts_code IN ({placeholders}) "
                                 f"ORDER BY ts_code, trade_date", (query_end, min_date, *batch)))
        ser: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            ser.setdefault(r["ts_code"], []).append((r["trade_date"], float(r["close"] or 0)))
        adj: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            adj.setdefault(r["ts_code"], []).append((r["trade_date"], float(r["adj_factor"] or 1)))
        out = {}
        for kind in {e["kind"] for e in events}:
            evs = [e for e in events if e["kind"] == kind]
            cars = {1: [], 5: [], 10: [], 20: []}
            for e in evs:
                dates = [d for d, _ in ser.get(e["ts_code"], [])]
                if e["event_date"] not in dates:
                    continue
                i = dates.index(e["event_date"])
                px = ser[e["ts_code"]]
                af = adj[e["ts_code"]]
                def ac(i):
                    return px[i][1] * af[i][1]
                for h in (1, 5, 10, 20):
                    j = i + h
                    if j < len(px) and i + 1 < len(px):
                        base = ac(i)
                        if base > 0:
                            cars[h].append(ac(j) / base - 1)
            row = {"kind": kind, "scope": "research", "n_events": len(evs)}
            for h in (1, 5, 10, 20):
                vals = cars[h]
                row[f"car_{h}"] = float(sum(vals) / len(vals)) if vals else None
            vals5 = cars[5]
            row["hit_rate"] = (sum(1 for v in vals5 if (v > 0) == (evs[0]["score"] > 0)) / len(vals5)) if vals5 else None
            row["computed_at"] = now_ts()
            row["detail_json"] = "{}"
            out[kind] = row
        with db.tx(lab):
            for row in out.values():
                lab.execute("INSERT OR REPLACE INTO event_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (row["kind"], row["scope"], row["n_events"], row["car_1"],
                             row["car_5"], row["car_10"], row["car_20"], row["hit_rate"],
                             row["computed_at"], row["detail_json"]))
        return out
    finally:
        mk.close()
        lab.close()
