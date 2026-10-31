# -*- coding: utf-8 -*-
"""Dataset sync jobs: vendor -> immutable RAW -> normalized.

Design:
- every fetch writes an immutable raw batch first (raw.db), then upserts
  normalized rows (market.db). RAW is append-only; normalized is derived.
- long loops (per trade date) checkpoint via the watermark table; a crashed
  run resumes from the last completed date (bounded retry inside client).
- vendor failures propagate as VendorError; the runtime marks the run FAILED
  and the pipeline degrades gracefully elsewhere.
"""
from __future__ import annotations

import time
from typing import Callable

import pandas as pd

from .. import db
from ..config import get_settings
from ..util import available_at_for_bar, get_logger, now_ts, sha1, to_date, today_str, ymd
from ..vendor import VendorError, available_sources, fetch, fetch_paged, get_client

log = get_logger("qforge.sync")

# ----------------------------------------------------------------------------
# raw writing


def write_raw(api: str, source: str, params: dict, rows: list[dict],
              date_key: str | None = None) -> str:
    """Append one immutable raw batch. batch_id is content+time unique."""
    raw = db.connect_raw()
    try:
        params_json = db.json_dumps({k: v for k, v in params.items() if not str(k).startswith("__")})
        batch_id = sha1([api, params_json, now_ts(), time.monotonic_ns()])[:24]
        recs = [{
            "api": api, "batch_id": batch_id, "seq": i,
            "ts_code": r.get("ts_code"),
            "trade_date": date_key or r.get("trade_date") or r.get("cal_date") or r.get("ann_date"),
            "payload_json": db.json_dumps(r),
        } for i, r in enumerate(rows)]
        with db.tx(raw):
            raw.execute(
                "INSERT INTO raw_meta (batch_id, api, source, params_json, request_id, fetched_at, row_count, status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, api, source, params_json, None, now_ts(), len(rows), "ok"))
            raw.executemany(
                "INSERT INTO raw_rows (api, batch_id, seq, ts_code, trade_date, payload_json)"
                " VALUES (?,?,?,?,?,?)",
                [(r["api"], r["batch_id"], r["seq"], r["ts_code"], r["trade_date"], r["payload_json"])
                 for r in recs])
        return batch_id
    finally:
        raw.close()


# ----------------------------------------------------------------------------
# generic per-date loop with watermark checkpoint


def _dates_between(cal_dates: list[str], start: str, end: str) -> list[str]:
    return [d for d in cal_dates if start <= ymd(d) <= end]


def _norm_upsert(conn, table: str, cols: list[str], rows: list[dict],
                 transform: Callable[[dict], dict | None]) -> int:
    out = []
    for r in rows:
        t = transform(r)
        if t:
            out.append(t)
    if out:
        db.upsert(conn, table, out, cols)
    return len(out)


def sync_per_date(api: str, dataset: str, start: str, end: str,
                  cols: list[str], transform: Callable[[dict], dict | None],
                  paged: bool = True, page: int = 5000,
                  required: bool = True, workers: int = 8,
                  recheck_days: int = 0) -> dict:
    """Fetch `api` for every open trade date in [start, end]; checkpointed.

    Dates are fetched concurrently (bounded workers, honoring the vendor IP
    budget) but committed strictly in order so the watermark only advances
    over a contiguous prefix -- resume is always exact. `recheck_days` forces
    the last N open days to be re-fetched even below the watermark (cheap,
    idempotent): covers sessions whose data was still unpublished at last sync.
    """
    from concurrent.futures import ThreadPoolExecutor
    s = get_settings()
    _cal_guard()
    mk = db.connect_market()
    try:
        cal = [r["trade_date"] for r in db.q(mk, "SELECT trade_date FROM cal WHERE is_open=1 ORDER BY trade_date")]
        wm = db.wm_get(mk, dataset, "last_date")
        todo = [d for d in cal if (not wm or d > wm) and start <= d <= end]
        if recheck_days and wm:
            recent = [d for d in cal if d <= min(end, wm)][-recheck_days:]
            todo = sorted(set(todo) | set(recent))
        if not todo:
            return {"dataset": dataset, "dates": 0, "rows": 0, "resumed_from": wm}
        log.info("sync %s: %d dates %s..%s (watermark %s, %d workers)", dataset,
                 len(todo), todo[0], todo[-1], wm, workers)

        def fetch_one(d: str):
            params = {"trade_date": d}
            if paged:
                rows, src = fetch_paged(api, params)
            else:
                rows, src = fetch(api, params)
            return d, params, rows, src

        total = 0
        done_i = -1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_one, d) for d in todo]
            for i, fut in enumerate(futures):
                try:
                    d, params, rows, src = fut.result()
                except Exception as e:
                    log.error("sync %s failed at %s: %s", dataset, todo[i], e)
                    break
                write_raw(api, src, params, rows, date_key=d)
                mk2 = db.connect_market()
                try:
                    total += _norm_upsert(mk2, _norm_table_of(dataset), cols, rows, transform)
                finally:
                    mk2.close()
                mk = db.connect_market()
                try:
                    cur = db.wm_get(mk, dataset, "last_date")
                    if not cur or d >= cur:  # watermark only advances (recheck days stay below)
                        db.wm_set(mk, dataset, "last_date", d)
                finally:
                    mk.close()
                done_i = i
                if (i + 1) % 50 == 0:
                    log.info("sync %s: %d/%d dates, %d rows", dataset, i + 1, len(todo), total)
        return {"dataset": dataset, "dates": done_i + 1, "rows": total, "resumed_from": wm}
    finally:
        try:
            mk.close()
        except Exception:
            pass


_NORM_TABLE = {
    "daily": "norm_daily", "daily_basic": "norm_daily_basic",
    "adj_factor": None, "stk_limit": "norm_stk_limit", "suspend_d": "norm_suspend",
}
_NORM_TABLE["adj_factor"] = "norm_daily"  # adj merged into norm_daily


def _norm_table_of(dataset: str) -> str:
    return _NORM_TABLE[dataset]


# ----------------------------------------------------------------------------
# individual datasets


def sync_trade_cal() -> dict:
    s = get_settings()
    rows, src = fetch("trade_cal", {"exchange": "SSE", "start_date": "19991201",
                                    "end_date": s.calendar_end})
    write_raw("trade_cal", src, {"exchange": "SSE"}, rows)
    mk = db.connect_market()
    try:
        out = [{"exchange": "SSE", "cal_date": r["cal_date"], "is_open": int(r.get("is_open", 0)),
                "pretrade_date": r.get("pretrade_date")} for r in rows]
        db.upsert(mk, "norm_trade_cal", out, ["exchange", "cal_date", "is_open", "pretrade_date"])
        # curated cal
        cal = [{"trade_date": r["cal_date"], "is_open": int(r.get("is_open", 0))} for r in rows]
        db.upsert(mk, "cal", cal, ["trade_date", "is_open"])
        n_open = sum(1 for c in cal if c["is_open"])
        return {"dataset": "trade_cal", "rows": len(cal), "open_days": n_open}
    finally:
        mk.close()


def cal_open(n: int) -> list[str]:
    """Last n open sessions up to today (for watermark-below rechecks)."""
    from ..data import calendar as cal_mod
    od = [d for d in cal_mod.open_days() if d <= today_str()]
    return od[-n:] if n and od else []


def _cal_guard():
    mk = db.connect_market()
    try:
        n = db.q1(mk, "SELECT COUNT(*) c FROM cal WHERE is_open=1")
        if not n or n["c"] == 0:
            raise RuntimeError("trade calendar empty; run sync trade_cal first")
    finally:
        mk.close()


def sync_stock_basic() -> dict:
    """All securities with list_status L/D/P -> sec_master inputs."""
    all_rows = []
    src_used = None
    for status in ("L", "D", "P"):
        try:
            rows, src = fetch_paged("stock_basic", {"list_status": status})
            for r in rows:
                r["_list_status"] = status
            all_rows.extend(rows)
            src_used = src_used or src
            log.info("stock_basic status=%s rows=%d (via %s)", status, len(rows), src)
        except VendorError as e:
            if status == "L":
                raise
            log.warning("stock_basic list_status=%s unavailable: %s", status, e)
    if not all_rows:
        raise VendorError("none", "stock_basic", "no securities returned")
    write_raw("stock_basic", src_used or "", {"list_status": "L,D,P"}, all_rows)
    mk = db.connect_market()
    try:
        out = [{
            "ts_code": r["ts_code"], "symbol": r.get("symbol"), "name": r.get("name"),
            "area": r.get("area"), "industry": r.get("industry"), "market": r.get("market"),
            "exchange": r.get("exchange"), "list_status": r.get("_list_status", r.get("list_status", "L")),
            "list_date": r.get("list_date"), "delist_date": r.get("delist_date"),
        } for r in all_rows]
        db.upsert(mk, "norm_stock_basic", out,
                  ["ts_code", "symbol", "name", "area", "industry", "market", "exchange",
                   "list_status", "list_date", "delist_date"])
        return {"dataset": "stock_basic", "rows": len(out),
                "delisted": sum(1 for o in out if o["list_status"] == "D")}
    finally:
        mk.close()


def sync_namechange() -> dict:
    try:
        rows, src = fetch_paged("namechange")
    except VendorError as e:
        log.warning("namechange unavailable (%s); ST history will fall back to current names", e)
        return {"dataset": "namechange", "rows": 0, "skipped": True}
    if not rows:
        return {"dataset": "namechange", "rows": 0, "skipped": True}
    write_raw("namechange", src, {}, rows)
    mk = db.connect_market()
    try:
        out = [{
            "ts_code": r["ts_code"], "name": r.get("name"), "start_date": r.get("start_date"),
            "end_date": r.get("end_date"), "ann_date": r.get("ann_date"),
            "change_reason": r.get("change_reason"),
        } for r in rows]
        db.upsert(mk, "norm_namechange", out,
                  ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"])
        return {"dataset": "namechange", "rows": len(out)}
    finally:
        mk.close()


def sync_daily(start: str | None = None, end: str | None = None,
               recheck_days: int = 0) -> dict:
    s = get_settings()
    _cal_guard()
    return sync_per_date(
        "daily", "daily", start or s.history_start, end or today_str(),
        ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
         "change", "pct_chg", "vol", "amount"],
        _merge_adj_transform("daily"), recheck_days=recheck_days)


def _merge_adj_transform(_which: str):
    def t(r: dict) -> dict | None:
        return {
            "ts_code": r.get("ts_code"), "trade_date": r.get("trade_date"),
            "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
            "close": r.get("close"), "pre_close": r.get("pre_close"),
            "change": r.get("change"), "pct_chg": r.get("pct_chg"),
            "vol": r.get("vol"), "amount": r.get("amount"),
        }
    return t


def sync_adj_factor(start: str | None = None, end: str | None = None,
                    recheck_days: int = 0) -> dict:
    """Fetch adj_factor per date and merge into norm_daily.adj_factor (concurrent)."""
    from concurrent.futures import ThreadPoolExecutor
    s = get_settings()
    _cal_guard()
    mk = db.connect_market()
    try:
        cal = [r["trade_date"] for r in db.q(mk, "SELECT trade_date FROM cal WHERE is_open=1 ORDER BY trade_date")]
        wm = db.wm_get(mk, "adj_factor", "last_date")
    finally:
        mk.close()
    todo = [d for d in cal if (not wm or d > wm) and (start or s.history_start) <= d <= (end or today_str())]
    if recheck_days:
        todo = sorted(set(todo) | set(cal_open(recheck_days)))
    if not todo:
        return {"dataset": "adj_factor", "dates": 0, "rows": 0, "resumed_from": wm}

    def fetch_one(d):
        params = {"trade_date": d}
        rows, src = fetch_paged("adj_factor", params)
        return d, params, rows, src

    total, done_i = 0, -1
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, d) for d in todo]
        for i, fut in enumerate(futures):
            try:
                d, params, rows, src = fut.result()
            except Exception as e:
                log.error("sync adj_factor failed at %s: %s", todo[i], e)
                break
            write_raw("adj_factor", src, params, rows, date_key=d)
            amap = {r["ts_code"]: r.get("adj_factor") for r in rows if r.get("ts_code")}
            mk = db.connect_market()
            try:
                with db.tx(mk):
                    for ts, af in amap.items():
                        mk.execute("UPDATE norm_daily SET adj_factor=? WHERE ts_code=? AND trade_date=?",
                                   (af, ts, d))
                        mk.execute("INSERT OR IGNORE INTO norm_daily (ts_code, trade_date, adj_factor) VALUES (?,?,?)",
                                   (ts, d, af))
                db.wm_set(mk, "adj_factor", "last_date", d)
            finally:
                mk.close()
            total += len(amap)
            done_i = i
            if (i + 1) % 50 == 0:
                log.info("sync adj_factor: %d/%d dates", i + 1, len(todo))
    return {"dataset": "adj_factor", "dates": done_i + 1, "rows": total, "resumed_from": wm}


def sync_daily_basic(start: str | None = None, end: str | None = None,
                     recheck_days: int = 0) -> dict:
    s = get_settings()
    if not s.fetch_daily_basic:
        return {"dataset": "daily_basic", "skipped": True}
    _cal_guard()
    return sync_per_date(
        "daily_basic", "daily_basic", start or s.history_start, end or today_str(),
        ["ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f", "volume_ratio",
         "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
         "total_share", "float_share", "free_share", "total_mv", "circ_mv"],
        lambda r: {
            "ts_code": r.get("ts_code"), "trade_date": r.get("trade_date"),
            "close": r.get("close"), "turnover_rate": r.get("turnover_rate"),
            "turnover_rate_f": r.get("turnover_rate_f"), "volume_ratio": r.get("volume_ratio"),
            "pe": r.get("pe"), "pe_ttm": r.get("pe_ttm"), "pb": r.get("pb"),
            "ps": r.get("ps"), "ps_ttm": r.get("ps_ttm"),
            "dv_ratio": r.get("dv_ratio"), "dv_ttm": r.get("dv_ttm"),
            "total_share": r.get("total_share"), "float_share": r.get("float_share"),
            "free_share": r.get("free_share"), "total_mv": r.get("total_mv"),
            "circ_mv": r.get("circ_mv"),
        }, recheck_days=recheck_days)


def sync_stk_limit(start: str | None = None, end: str | None = None,
                   recheck_days: int = 0) -> dict:
    s = get_settings()
    if not s.fetch_stk_limit:
        return {"dataset": "stk_limit", "skipped": True}
    _cal_guard()
    return sync_per_date(
        "stk_limit", "stk_limit", start or s.history_start, end or today_str(),
        ["ts_code", "trade_date", "pre_close", "up_limit", "down_limit"],
        lambda r: {
            "ts_code": r.get("ts_code"), "trade_date": r.get("trade_date"),
            "pre_close": r.get("pre_close"), "up_limit": r.get("up_limit"),
            "down_limit": r.get("down_limit"),
        }, recheck_days=recheck_days)


def sync_suspend(start: str | None = None, end: str | None = None,
                 recheck_days: int = 0) -> dict:
    """Full-market suspension calendar. suspend_d requires a date param; we walk
    trade dates. Missing vendor table is non-fatal (bars-based inference covers it)."""
    s = get_settings()
    if not s.fetch_suspend:
        return {"dataset": "suspend_d", "skipped": True}
    _cal_guard()
    recheck = [d for d in cal_open(recheck_days)] if recheck_days else []
    mk = db.connect_market()
    try:
        cal = [r["trade_date"] for r in db.q(mk, "SELECT trade_date FROM cal WHERE is_open=1 ORDER BY trade_date")]
        wm = db.wm_get(mk, "suspend_d", "last_date")
    finally:
        mk.close()
    todo = [d for d in cal if (not wm or d > wm) and (start or s.history_start) <= d <= (end or today_str())]
    todo = sorted(set(todo) | set(recheck))
    total, got_any = 0, False
    for i, d in enumerate(todo):
        try:
            rows, src = fetch_paged("suspend_d", {"suspend_date": d})
        except VendorError:
            try:
                rows, src = fetch_paged("suspend_d", {"trade_date": d})
            except VendorError as e:
                log.warning("suspend_d unavailable (%s); inference from bars will be used", e)
                return {"dataset": "suspend_d", "skipped": True, "reason": str(e)[:120]}
        if rows:
            got_any = True
        write_raw("suspend_d", src, {"suspend_date": d}, rows, date_key=d)
        out = [{"ts_code": r.get("ts_code"), "trade_date": d,
                "suspend_type": r.get("suspend_type")} for r in rows if r.get("ts_code")]
        mk = db.connect_market()
        try:
            if out:
                db.upsert(mk, "norm_suspend", out, ["ts_code", "trade_date", "suspend_type"])
            db.wm_set(mk, "suspend_d", "last_date", d)
        finally:
            mk.close()
        total += len(out)
    return {"dataset": "suspend_d", "dates": len(todo), "rows": total, "has_data": got_any}


def sync_index_daily(codes: list[str] | None = None, start: str | None = None,
                     end: str | None = None) -> dict:
    s = get_settings()
    codes = list(codes or s.benchmark_codes)
    start = start or "20050101"
    end = end or today_str()
    total = 0
    for code in codes:
        rows, src = fetch_paged("index_daily", {"ts_code": code, "start_date": start, "end_date": end})
        write_raw("index_daily", src, {"ts_code": code, "start_date": start, "end_date": end}, rows)
        out = [{
            "ts_code": r.get("ts_code"), "trade_date": r.get("trade_date"),
            "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
            "close": r.get("close"), "pre_close": r.get("pre_close"),
            "vol": r.get("vol"), "amount": r.get("amount"),
        } for r in rows]
        mk = db.connect_market()
        try:
            db.upsert(mk, "norm_index_daily", out,
                      ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"])
        finally:
            mk.close()
        total += len(out)
        log.info("index_daily %s rows=%d", code, len(out))
    return {"dataset": "index_daily", "codes": codes, "rows": total}


def sync_fund_basic() -> dict:
    try:
        rows, src = fetch_paged("fund_basic", {"market": "E"})
    except VendorError as e:
        log.warning("fund_basic unavailable: %s", e)
        return {"dataset": "fund_basic", "skipped": True}
    write_raw("fund_basic", src, {"market": "E"}, rows)
    mk = db.connect_market()
    try:
        out = [{
            "ts_code": r.get("ts_code"), "symbol": r.get("symbol"), "name": r.get("name"),
            "fund_type": r.get("fund_type"), "found_date": r.get("found_date"),
            "list_date": r.get("list_date"), "delist_date": r.get("delist_date"),
            "management": r.get("management"), "status": r.get("status"), "market": r.get("market"),
        } for r in rows]
        db.upsert(mk, "norm_fund_basic", out,
                  ["ts_code", "symbol", "name", "fund_type", "found_date", "list_date",
                   "delist_date", "management", "status", "market"])
        return {"dataset": "fund_basic", "rows": len(out)}
    finally:
        mk.close()


def sync_etf_daily(codes: list[str] | None = None, start: str | None = None,
                   end: str | None = None) -> dict:
    s = get_settings()
    codes = list(codes or s.etf_pool)
    start = start or s.history_start
    end = end or today_str()
    total = 0
    for code in codes:
        try:
            rows, src = fetch_paged("fund_daily", {"ts_code": code, "start_date": start, "end_date": end})
        except VendorError as e:
            log.warning("fund_daily %s failed: %s", code, e)
            continue
        write_raw("fund_daily", src, {"ts_code": code, "start_date": start, "end_date": end}, rows)
        out = [{
            "ts_code": r.get("ts_code"), "trade_date": r.get("trade_date"),
            "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
            "close": r.get("close"), "pre_close": r.get("pre_close"),
            "vol": r.get("vol"), "amount": r.get("amount"),
        } for r in rows]
        # ETF adjustment factor (dividends) -- optional
        try:
            adjrows, _ = fetch_paged("fund_adj", {"ts_code": code, "start_date": start, "end_date": end})
            write_raw("fund_adj", src, {"ts_code": code}, adjrows)
            adjmap = {r["trade_date"]: r.get("adj_factor") for r in adjrows if r.get("trade_date")}
            for o in out:
                o["adj_factor"] = adjmap.get(o["trade_date"])
        except VendorError:
            for o in out:
                o["adj_factor"] = None
        mk = db.connect_market()
        try:
            db.upsert(mk, "norm_fund_daily", out,
                      ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                       "vol", "amount", "adj_factor"])
        finally:
            mk.close()
        total += len(out)
        log.info("fund_daily %s rows=%d", code, len(out))
    return {"dataset": "etf_daily", "codes": codes, "rows": total}


def sync_fina_indicator(codes: list[str] | None = None, max_codes: int | None = None) -> dict:
    """Optional per-stock fundamentals (PIT via ann_date). Rolling enrichment job."""
    s = get_settings()
    if not s.fetch_fundamentals and not codes:
        return {"dataset": "fina_indicator", "skipped": True}
    mk = db.connect_market()
    try:
        if codes:
            todo = list(codes)
        else:
            have = {r["ts_code"] for r in db.q(mk, "SELECT DISTINCT ts_code FROM norm_fina_indicator")}
            allc = [r["ts_code"] for r in db.q(mk, "SELECT ts_code FROM norm_stock_basic ORDER BY ts_code")]
            todo = [c for c in allc if c not in have]
    finally:
        mk.close()
    todo = todo[: max_codes or 300]
    total = 0
    for code in todo:
        try:
            rows, src = fetch_paged("fina_indicator", {"ts_code": code})
        except VendorError as e:
            log.warning("fina_indicator %s failed: %s", code, e)
            continue
        write_raw("fina_indicator", src, {"ts_code": code}, rows)
        out = [{
            "ts_code": r.get("ts_code"), "end_date": r.get("end_date"),
            "ann_date": r.get("ann_date"), "f_ann_date": r.get("f_ann_date"),
            "roe": r.get("roe"), "roe_dt": r.get("roe_dt"),
            "grossprofit_margin": r.get("grossprofit_margin"),
            "netprofit_margin": r.get("netprofit_margin"),
            "debt_to_assets": r.get("debt_to_assets"), "ocf_to_or": r.get("ocf_to_or"),
            "eps": r.get("eps"), "bps": r.get("bps"),
            "or_yoy": r.get("or_yoy"), "netprofit_yoy": r.get("netprofit_yoy"),
            "extra_json": db.json_dumps({k: v for k, v in r.items()
                                         if k not in ("ts_code", "end_date", "ann_date", "f_ann_date")}),
        } for r in rows if r.get("end_date")]
        mk = db.connect_market()
        try:
            db.upsert(mk, "norm_fina_indicator", out,
                      ["ts_code", "end_date", "ann_date", "f_ann_date", "roe", "roe_dt",
                       "grossprofit_margin", "netprofit_margin", "debt_to_assets", "ocf_to_or",
                       "eps", "bps", "or_yoy", "netprofit_yoy", "extra_json"])
        finally:
            mk.close()
        total += len(out)
    return {"dataset": "fina_indicator", "codes": len(todo), "rows": total}


def sync_dividend(codes: list[str] | None = None, max_codes: int | None = None) -> dict:
    if not codes:
        return {"dataset": "dividend", "skipped": True}
    total = 0
    for code in codes:
        try:
            rows, src = fetch("dividend", {"ts_code": code})
        except VendorError as e:
            log.warning("dividend %s failed: %s", code, e)
            continue
        if not rows:
            continue
        write_raw("dividend", src, {"ts_code": code}, rows)
        out = [{
            "ts_code": code, "end_date": r.get("end_date"), "ann_date": r.get("ann_date"),
            "div_proc": r.get("div_proc"), "stk_div": r.get("stk_div"),
            "stk_bo_rate": _f(r.get("stk_bo_rate")), "stk_co_rate": _f(r.get("stk_co_rate")),
            "cash_div": _f(r.get("cash_div")), "cash_div_tax": _f(r.get("cash_div_tax")),
            "base_share": _f(r.get("base_share")), "ex_date": r.get("ex_date"),
            "pay_date": r.get("pay_date"),
        } for r in rows if r.get("end_date")]
        mk = db.connect_market()
        try:
            db.upsert(mk, "norm_dividend", out,
                      ["ts_code", "end_date", "ann_date", "div_proc", "stk_div", "stk_bo_rate",
                       "stk_co_rate", "cash_div", "cash_div_tax", "base_share", "ex_date", "pay_date"])
        finally:
            mk.close()
        total += len(out)
    return {"dataset": "dividend", "codes": len(codes), "rows": total}


def _f(v):
    try:
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def sync_announcements(start: str | None = None, end: str | None = None) -> dict:
    """anns_d per calendar day (promax). Rolling window for Event Intelligence."""
    import datetime as _dt
    s = get_settings()
    end = end or today_str()
    start = start or (to_date(end) - _dt.timedelta(days=s.fetch_events_window_days)).strftime("%Y%m%d")
    mk = db.connect_market()
    try:
        wm = db.wm_get(mk, "anns_d", "last_date")
    finally:
        mk.close()
    # announcements also arrive on non-trade days: walk calendar days
    from ..util import daterange_days
    todo = [d for d in daterange_days(start, end) if (not wm or d > wm)]
    total = 0
    for i, d in enumerate(todo):
        try:
            rows, src = fetch_paged("anns_d", {"ann_date": d})
        except VendorError as e:
            log.warning("anns_d %s failed: %s", d, e)
            return {"dataset": "anns_d", "dates": i, "rows": total, "resumed_from": wm}
        if rows:
            write_raw("anns_d", src, {"ann_date": d}, rows, date_key=d)
        out = [{
            "ann_id": sha1([r.get("ts_code"), r.get("ann_date"), r.get("title"), r.get("name")]),
            "ts_code": r.get("ts_code"), "name": r.get("name"), "ann_date": d,
            "title": r.get("title"),
        } for r in rows if r.get("ts_code")]
        mk = db.connect_market()
        try:
            if out:
                db.upsert(mk, "norm_anns", out, ["ann_id", "ts_code", "name", "ann_date", "title"])
            db.wm_set(mk, "anns_d", "last_date", d)
        finally:
            mk.close()
        total += len(out)
        if (i + 1) % 10 == 0:
            log.info("sync anns_d: %d/%d days, %d anns", i + 1, len(todo), total)
    return {"dataset": "anns_d", "dates": len(todo), "rows": total}


def sync_news(start: str | None = None, end: str | None = None) -> dict:
    """news via promax (src=cls). Window-based, watermark on end datetime."""
    import datetime as _dt
    s = get_settings()
    end = end or today_str()
    start = start or (to_date(end) - _dt.timedelta(days=s.fetch_events_window_days)).strftime("%Y%m%d")
    params = {"src": "cls",
              "start_date": f"{start[:4]}-{start[4:6]}-{start[6:]} 00:00:00",
              "end_date": f"{end[:4]}-{end[4:6]}-{end[6:]} 23:59:59"}
    try:
        rows, src = fetch_paged("news", params)
    except VendorError as e:
        log.warning("news unavailable: %s", e)
        return {"dataset": "news", "skipped": True, "reason": str(e)[:120]}
    write_raw("news", src, params, rows)
    out = []
    for r in rows:
        content = (r.get("content") or "")
        nid = sha1([r.get("datetime"), content[:200], r.get("title") or ""])
        out.append({"nid": nid, "datetime": r.get("datetime"), "src": r.get("src") or "cls",
                    "title": r.get("title"), "content": content})
    mk = db.connect_market()
    try:
        db.upsert(mk, "norm_news", out, ["nid", "datetime", "src", "title", "content"])
    finally:
        mk.close()
    return {"dataset": "news", "rows": len(out)}


# ----------------------------------------------------------------------------
# orchestration


JOBS: dict[str, Callable[..., dict]] = {
    "trade_cal": sync_trade_cal,
    "stock_basic": sync_stock_basic,
    "namechange": sync_namechange,
    "daily": sync_daily,
    "adj_factor": sync_adj_factor,
    "daily_basic": sync_daily_basic,
    "stk_limit": sync_stk_limit,
    "suspend_d": sync_suspend,
    "index_daily": sync_index_daily,
    "fund_basic": sync_fund_basic,
    "etf_daily": sync_etf_daily,
    "anns_d": sync_announcements,
    "news": sync_news,
}


def run_sync(dataset: str, start: str | None = None, end: str | None = None,
             codes: list[str] | None = None, recheck_days: int = 0) -> dict:
    if not available_sources():
        raise VendorError("none", dataset, "no vendor credentials configured")
    if dataset == "fina_indicator":
        return sync_fina_indicator(codes=codes)
    if dataset == "dividend":
        return sync_dividend(codes=codes)
    job = JOBS.get(dataset)
    if not job:
        raise ValueError(f"unknown sync dataset: {dataset}")
    import inspect
    kw = {}
    sig = inspect.signature(job)
    if start is not None and "start" in sig.parameters:
        kw["start"] = start
    if end is not None and "end" in sig.parameters:
        kw["end"] = end
    if "recheck_days" in sig.parameters:
        kw["recheck_days"] = recheck_days
    return job(**kw)
