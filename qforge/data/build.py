# -*- coding: utf-8 -*-
"""Curated layer builder: normalized -> PIT-facing tables.

Everything here is derived and fully rebuildable (INSERT OR REPLACE):
  cal (already built in sync) / sec_master / name_hist / bars / corp_action
  univ_daily / bench_daily / etf_daily

Historical universe rule: membership is derived strictly from list_date,
delist_date, name history (ST), suspension records and bar presence -- never
from today's listing state.
"""
from __future__ import annotations

import datetime as dt

from .. import db
from ..config import get_settings
from ..util import get_logger, now_ts, ymd

log = get_logger("qforge.build")


# ---------------------------------------------------------------- boards ----
def classify_board(ts_code: str, market: str | None = None) -> str:
    code, _, exch = ts_code.partition(".")
    exch = exch.upper()
    if exch == "BJ":
        return "BSE"
    if market in ("科创板", "STAR"):
        return "STAR"
    if market in ("创业板", "CHINEXT"):
        return "CHINEXT"
    if exch == "SH":
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith("9"):
            return "BSHARE"
        return "MAIN"
    if exch == "SZ":
        if code.startswith(("300", "301", "302")):
            return "CHINEXT"
        if code.startswith("20"):
            return "BSHARE"
        return "MAIN"
    return "OTHER"


def is_st_name(name: str | None) -> bool:
    if not name:
        return False
    n = name.upper()
    return "ST" in n or "退" in n


def limit_pct_for(board: str, is_st: bool, trade_date: str) -> float:
    """Price limit ratio. A-share rules:
    main board 10% (ST 5%); ChiNext/STAR 20% (since 2020-08-24 for ChiNext,
    since inception for STAR; before that ChiNext was 10%); BSE 30%.
    """
    d = ymd(trade_date)
    if board in ("CHINEXT",):
        return 0.10 if d < "20200824" else 0.20
    if board == "STAR":
        return 0.20
    if board == "BSE":
        return 0.30
    # MAIN / BSHARE
    return 0.05 if is_st else 0.10


# ---------------------------------------------------------------- builders ----
def build_sec_master() -> dict:
    mk = db.connect_market()
    try:
        rows = db.q(mk, "SELECT * FROM norm_stock_basic")
        out = []
        for r in rows:
            out.append({
                "ts_code": r["ts_code"], "symbol": r["symbol"], "name": r["name"],
                "exchange": r["exchange"] or r["ts_code"].split(".")[-1],
                "board": classify_board(r["ts_code"], r["market"]),
                "sec_type": "STOCK", "list_date": r["list_date"],
                "delist_date": r["delist_date"], "list_status": r["list_status"],
            })
        # ETFs into the same master (rotation strategies)
        for r in db.q(mk, "SELECT * FROM norm_fund_basic WHERE market='E'"):
            out.append({
                "ts_code": r["ts_code"], "symbol": r["symbol"], "name": r["name"],
                "exchange": r["ts_code"].split(".")[-1], "board": "FUND",
                "sec_type": "ETF", "list_date": r["list_date"],
                "delist_date": r["delist_date"], "list_status": r["status"] or "L",
            })
        n = db.upsert(mk, "sec_master", out, ["ts_code", "symbol", "name", "exchange",
                                              "board", "sec_type", "list_date", "delist_date",
                                              "list_status"])
        return {"table": "sec_master", "rows": n}
    finally:
        mk.close()


def build_name_hist() -> dict:
    """ST flag history from name changes, merged with a baseline current-name
    row for securities the namechange feed does not cover (vendor limitation:
    the relay currently exposes only a tiny slice of namechange history)."""
    mk = db.connect_market()
    try:
        rows = db.q(mk, "SELECT * FROM norm_namechange ORDER BY ts_code, start_date")
        out = []
        covered = set()
        if rows:
            for r in rows:
                covered.add(r["ts_code"])
                out.append({"ts_code": r["ts_code"], "start_date": r["start_date"] or "19900101",
                            "end_date": r["end_date"], "name": r["name"],
                            "is_st": int(is_st_name(r["name"]))})
        for r in db.q(mk, "SELECT ts_code, name, list_date FROM norm_stock_basic"):
            if r["ts_code"] in covered:
                continue
            out.append({"ts_code": r["ts_code"], "start_date": r["list_date"] or "19900101",
                        "end_date": None, "name": r["name"],
                        "is_st": int(is_st_name(r["name"]))})
        n = db.upsert(mk, "name_hist", out, ["ts_code", "start_date", "end_date", "name", "is_st"])
        return {"table": "name_hist", "rows": n, "from_namechange": bool(rows)}
    finally:
        mk.close()


def build_bars() -> dict:
    """Curated bars: raw OHLC + adj_factor + official limits (fallback computed)
    + suspension + ST flag + board."""
    s = get_settings()
    mk = db.connect_market()
    try:
        boards = {r["ts_code"]: r["board"] for r in db.q(mk, "SELECT ts_code, board FROM sec_master WHERE sec_type='STOCK'")}
        # name history -> per (ts_code, date) is_st resolved in SQL below
        mk.execute("DROP TABLE IF EXISTS _tmp_st")
        mk.executescript("""
            CREATE TABLE _tmp_st (ts_code TEXT, start_date TEXT, end_date TEXT, is_st INT);
            CREATE INDEX _tmp_st_ix ON _tmp_st(ts_code, start_date);
        """)
        mk.executemany("INSERT INTO _tmp_st VALUES (?,?,?,?)",
                       [(r["ts_code"], r["start_date"] or "19900101", r["end_date"], r["is_st"])
                        for r in db.q(mk, "SELECT ts_code, start_date, end_date, is_st FROM name_hist")])
        sql = """
        SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close, d.pre_close,
               d.vol, d.amount, d.adj_factor,
               l.up_limit, l.down_limit,
               COALESCE((SELECT is_st FROM _tmp_st t
                         WHERE t.ts_code = d.ts_code AND t.start_date <= d.trade_date
                           AND (t.end_date IS NULL OR t.end_date >= d.trade_date)
                         ORDER BY t.start_date DESC LIMIT 1), 0) AS is_st,
               CASE WHEN s.trade_date IS NULL THEN 0 ELSE 1 END AS is_susp
        FROM norm_daily d
        LEFT JOIN norm_stk_limit l ON l.ts_code = d.ts_code AND l.trade_date = d.trade_date
        LEFT JOIN norm_suspend s ON s.ts_code = d.ts_code AND s.trade_date = d.trade_date
        """
        cols = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                "vol", "amount", "adj_factor", "up_limit", "down_limit", "is_st", "is_susp"]
        batch = []
        n = 0
        for row in mk.execute(sql):
            r = dict(zip(cols, row))
            board = boards.get(r["ts_code"], "OTHER")
            limit_source = "vendor"
            is_st = bool(r["is_st"])
            if r["up_limit"] is None and r["pre_close"]:
                pct = limit_pct_for(board, is_st, r["trade_date"])
                pc = r["pre_close"]
                # exchange rounding: tick 0.01 (SH/SZ main), STAR/CHINEXT 0.01
                r["up_limit"] = round(pc * (1 + pct) + 1e-9, 2)
                r["down_limit"] = round(pc * (1 - pct) + 1e-9, 2)
                limit_source = "computed"
            elif r["up_limit"] is None:
                r["up_limit"] = r["down_limit"] = None
                limit_source = "none"
            # official stk_limit corroborates ST: main-board 5% band => ST that day
            if (board in ("MAIN", "BSHARE") and r["up_limit"] and r["pre_close"]
                    and abs(r["up_limit"] / r["pre_close"] - 1.05) < 0.002):
                is_st = True
                if limit_source == "vendor":
                    r["down_limit"] = r["down_limit"] or round(r["pre_close"] * 0.95, 2)
            batch.append({
                "ts_code": r["ts_code"], "trade_date": r["trade_date"],
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "pre_close": r["pre_close"], "vol": r["vol"], "amount": r["amount"],
                "adj_factor": r["adj_factor"], "up_limit": r["up_limit"],
                "down_limit": r["down_limit"], "limit_source": limit_source,
                "is_suspended": r["is_susp"], "is_st": int(is_st), "board": board,
            })
            if len(batch) >= 20000:
                n += db.upsert(mk, "bars", batch,
                               ["ts_code", "trade_date", "open", "high", "low", "close",
                                "pre_close", "vol", "amount", "adj_factor", "up_limit",
                                "down_limit", "limit_source", "is_suspended", "is_st", "board"])
                batch = []
        n += db.upsert(mk, "bars", batch,
                       ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                        "vol", "amount", "adj_factor", "up_limit", "down_limit",
                        "limit_source", "is_suspended", "is_st", "board"])
        mk.execute("DROP TABLE IF EXISTS _tmp_st")
        return {"table": "bars", "rows": n}
    finally:
        mk.close()


def build_corp_actions() -> dict:
    """Ex-dates from adj_factor jumps; enrich with dividend table when present."""
    mk = db.connect_market()
    try:
        mk.execute("DELETE FROM corp_action")
        rows = mk.execute("""
            SELECT ts_code, trade_date, adj_factor,
                   LAG(adj_factor) OVER (PARTITION BY ts_code ORDER BY trade_date) prev_af
            FROM norm_daily WHERE adj_factor IS NOT NULL
        """).fetchall()
        div_by_key: dict[tuple, dict] = {}
        for r in db.q(mk, "SELECT * FROM norm_dividend WHERE ex_date IS NOT NULL"):
            div_by_key[(r["ts_code"], r["ex_date"])] = dict(r)
        out = []
        for r in rows:
            if r["prev_af"] is None or r["adj_factor"] is None:
                continue
            if abs(r["adj_factor"] - r["prev_af"]) < 1e-9:
                continue
            d = div_by_key.get((r["ts_code"], r["trade_date"]), {})
            ratio = r["adj_factor"] / r["prev_af"]
            out.append({
                "ts_code": r["ts_code"], "ex_date": r["trade_date"],
                "cash_div": d.get("cash_div") or 0.0,
                "stk_ratio": d.get("stk_bo_rate") if d.get("stk_bo_rate") else max(0.0, ratio - 1.0),
                "adj_before": r["prev_af"], "adj_after": r["adj_factor"],
                "source": "adj_factor+dividend" if d else "adj_factor",
            })
        n = db.upsert(mk, "corp_action", out,
                      ["ts_code", "ex_date", "cash_div", "stk_ratio", "adj_before",
                       "adj_after", "source"])
        return {"table": "corp_action", "rows": n}
    finally:
        mk.close()


def build_univ_daily() -> dict:
    """Historical universe per date. RESEARCH_ONLY vs PRODUCTION_ELIGIBLE."""
    s = get_settings()
    mk = db.connect_market()
    try:
        cal = [r["trade_date"] for r in db.q(mk, "SELECT trade_date FROM cal WHERE is_open=1 ORDER BY trade_date")]
        secs = db.q(mk, "SELECT ts_code, list_date, delist_date, board, sec_type FROM sec_master WHERE sec_type='STOCK'")
        susp = {(r["ts_code"], r["trade_date"]) for r in db.q(mk, "SELECT ts_code, trade_date FROM norm_suspend")}
        bar_dates: dict[str, set] = {}
        for r in db.q(mk, "SELECT DISTINCT ts_code, trade_date FROM norm_daily"):
            bar_dates.setdefault(r["ts_code"], set()).add(r["trade_date"])
        st_ranges: dict[str, list] = {}
        for r in db.q(mk, "SELECT ts_code, start_date, end_date, is_st FROM name_hist WHERE is_st=1"):
            st_ranges.setdefault(r["ts_code"], []).append((r["start_date"] or "19900101", r["end_date"]))
        # corroborate with official 5%-band evidence from curated bars
        st_from_bars = {(r["ts_code"], r["trade_date"]) for r in
                        db.q(mk, "SELECT ts_code, trade_date FROM bars WHERE is_st=1")}

        def st_on(code: str, d: str) -> bool:
            if (code, d) in st_from_bars:
                return True
            for a, b in st_ranges.get(code, []):
                if a <= d and (b is None or d <= b):
                    return True
            return False

        out = []
        for d in cal:
            for sec in secs:
                ld, dd = sec["list_date"], sec["delist_date"]
                if not ld or d < ld:
                    continue
                if dd and d > dd:
                    continue
                code = sec["ts_code"]
                has_bar = d in bar_dates.get(code, ())
                explicit_susp = (code, d) in susp
                is_susp = bool(explicit_susp or (not has_bar))
                listed_days = (dt.datetime.strptime(d, "%Y%m%d") - dt.datetime.strptime(ld, "%Y%m%d")).days
                st = st_on(code, d)
                reasons = []
                if is_susp:
                    reasons.append("SUSPENDED")
                if st:
                    reasons.append("ST")
                if listed_days < s.new_stock_min_days:
                    reasons.append("NEW")
                if sec["board"] in ("BSE", "BSHARE", "OTHER"):
                    reasons.append("BOARD")
                if not has_bar:
                    reasons.append("NO_BAR")
                elig_res = int(has_bar and not is_susp)
                prod_excluded = bool({"ST", "NEW", "BOARD", "NO_BAR", "SUSPENDED"} & set(reasons))
                elig_prod = int(elig_res and not prod_excluded)
                out.append({
                    "trade_date": d, "ts_code": code, "board": sec["board"], "sec_type": "STOCK",
                    "is_listed": 1, "is_suspended": int(is_susp), "is_st": int(st),
                    "is_new": int(listed_days < s.new_stock_min_days), "listed_days": listed_days,
                    "eligible_research": elig_res, "eligible_production": elig_prod,
                    "reasons": db.json_dumps(reasons),
                })
            if len(out) >= 100000:
                db.upsert(mk, "univ_daily", out,
                          ["trade_date", "ts_code", "board", "sec_type", "is_listed",
                           "is_suspended", "is_st", "is_new", "listed_days",
                           "eligible_research", "eligible_production", "reasons"])
                out = []
        db.upsert(mk, "univ_daily", out,
                  ["trade_date", "ts_code", "board", "sec_type", "is_listed", "is_suspended",
                   "is_st", "is_new", "listed_days", "eligible_research",
                   "eligible_production", "reasons"])
        cnt = db.q1(mk, "SELECT COUNT(*) c FROM univ_daily")
        return {"table": "univ_daily", "rows": cnt["c"]}
    finally:
        mk.close()


def build_benchmarks() -> dict:
    mk = db.connect_market()
    try:
        mk.execute("DELETE FROM bench_daily")
        n = mk.execute("""
            INSERT INTO bench_daily (ts_code, trade_date, open, high, low, close, pre_close, vol, amount)
            SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount
            FROM norm_index_daily
        """).rowcount
        m = mk.execute("""
            INSERT OR REPLACE INTO etf_daily (ts_code, trade_date, open, high, low, close, pre_close, vol, amount, adj_factor)
            SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount, adj_factor
            FROM norm_fund_daily
        """).rowcount
        return {"bench_daily": n, "etf_daily": m}
    finally:
        mk.close()


def build_etf_bars() -> dict:
    """ETF bars into the unified `bars` table (board=FUND) so the engine can
    trade them. Limits: 20% for ChiNext/STAR-tracking ETFs, else 10%."""
    mk = db.connect_market()
    try:
        names = {r["ts_code"]: (r["name"] or "") for r in db.q(mk, "SELECT ts_code, name FROM norm_fund_basic")}
        rows = db.q(mk, "SELECT * FROM norm_fund_daily")
        out = []
        for r in rows:
            nm = names.get(r["ts_code"], "")
            pct = 0.20 if (("创业" in nm) or ("科创" in nm) or ("双创" in nm)) else 0.10
            pc = r["pre_close"]
            up = dn = None
            if pc:
                up, dn = round(pc * (1 + pct) + 1e-9, 3), round(pc * (1 - pct) + 1e-9, 3)
            out.append({
                "ts_code": r["ts_code"], "trade_date": r["trade_date"],
                "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "pre_close": pc, "vol": r["vol"], "amount": r["amount"],
                "adj_factor": r["adj_factor"], "up_limit": up, "down_limit": dn,
                "limit_source": "computed", "is_suspended": 0, "is_st": 0, "board": "FUND",
            })
        n = db.upsert(mk, "bars", out, ["ts_code", "trade_date", "open", "high", "low", "close",
                                        "pre_close", "vol", "amount", "adj_factor", "up_limit",
                                        "down_limit", "limit_source", "is_suspended", "is_st", "board"])
        return {"etf_bars": n}
    finally:
        mk.close()


def build_all() -> dict:
    out = {}
    out["sec_master"] = build_sec_master()
    out["name_hist"] = build_name_hist()
    out["bars"] = build_bars()
    out["corp_action"] = build_corp_actions()
    out["univ_daily"] = build_univ_daily()
    out["benchmarks"] = build_benchmarks()
    out["etf_bars"] = build_etf_bars()
    out["built_at"] = now_ts()
    mk = db.connect_market()
    try:
        db.kv_set(mk, "curated_built_at", out["built_at"])
    finally:
        mk.close()
    log.info("curated build done: %s", out)
    return out
