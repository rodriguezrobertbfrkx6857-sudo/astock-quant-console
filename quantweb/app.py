# -*- coding: utf-8 -*-
"""量化分析网站 — FastAPI 后端 (LOCAL ONLY, 127.0.0.1)。

实现 React 前端约定的只读 API 契约。历史展示数据通过主项目内的
immutable 本地证据归档读取，查询进程无 DDL、WAL 或建库权限:

  /api/overview            概览: 版本/边界/数据覆盖
  /api/research/factors    因子研究 (factor_registry + factor_stats)
  /api/research/strategies 策略评级 (strategy_registry + evaluations)
  /api/research/backtests  回测列表 + 净值曲线 (backtests + bt_nav)
  /api/research-queue      研究队列 (research_queue)
  /api/market/daily        日线 OHLCV (etf_daily → bars → bench_daily 依次查找)
  /api/market/securities   证券列表/搜索 (sec_master + ETF, 支持 q 模糊)
  /api/paper/portfolio     PAPER 模拟盘 (paper_account/positions/nav)
  /api/events              事件情报 (event_store, 支持 kind 过滤 + 效果统计)
  /api/data-health         数据健康 (data_health + 去重代码数)
  /api/system              系统状态 (run_ledger)

性能: 重查询(亿级行计数/去重)经 TTL 缓存 + 后台线程预热与刷新,
页面请求永远读缓存, 秒级响应。另托管 dashboard/dist (SPA 回退)。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from qforge import __version__ as qforge_version
from qforge import db
from qforge.config import (
    ROOT,
    assert_safety,
    get_settings,
)
from src.quantcore.contracts.config import load_v2_config
from src.quantcore.data.hashing import canonical_hash
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService
from src.quantcore.contracts.safety import (
    ACCOUNT_ACCESS,
    BROKER_ACCESS,
    BROKER_AUTH,
    LOCAL_ONLY,
    REAL_ORDER_ENABLED,
    REAL_TRADING_ENABLED,
)

from . import __version__
from src.quantcore.compat.local_repository import LocalSQLiteRepository
from .v2_api import router as v2_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("quantweb")

PROJECT_NAME = "AStock Quant Web"
WEBSITE_VERSION = __version__
LEGACY_SOURCE_STATUS = "LOCAL_ARCHIVE"


# ── TTL 缓存 + 后台预热 (重查询只跑在后台线程, 请求永远读缓存) ──────────

_CACHE_TTL_SEC = 600  # 数据每个交易日只变一次, 10 分钟刷新绰绰有余
_cache: Dict[str, tuple] = {}  # key -> (computed_at_ts, value)
_cache_lock = threading.Lock()


def _cached(key: str, compute: Callable[[], Any]) -> Any:
    """读缓存; 首次未命中时同步计算一次 (之后由后台线程刷新)。"""
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit[1]
    val = _safe_compute(key, compute)
    if val is not None:
        with _cache_lock:
            _cache[key] = (time.time(), val)
    return val


def _safe_compute(key: str, compute: Callable[[], Any]) -> Any | None:
    t0 = time.time()
    try:
        val = compute()
        logger.info("cache[%s] computed in %.1fs", key, time.time() - t0)
        return val
    except Exception:
        logger.exception("cache[%s] compute failed", key)
        return None


def _refresh_loop() -> None:
    """后台守护线程: 启动即预热, 之后每 TTL 刷新一次 (stale-while-revalidate)。"""
    time.sleep(0.5)  # 等 uvicorn 起来
    while True:
        for key, compute in _HEAVY_QUERIES.items():
            with _cache_lock:
                hit = _cache.get(key)
                if hit is not None and time.time() - hit[0] < _CACHE_TTL_SEC:
                    continue
            val = _safe_compute(key, compute)
            if val is not None:
                with _cache_lock:
                    _cache[key] = (time.time(), val)
        time.sleep(60)


app = FastAPI(
    title=f"{PROJECT_NAME} API",
    version=WEBSITE_VERSION,
    description="量化分析网站 · 本地只读研究看板 (127.0.0.1 only)",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8888",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(v2_router)


# ── 数据连接 (只读 URI + SQLite authorizer; 不执行 qforge DDL) ──────────


def _read_repository() -> LocalSQLiteRepository:
    return LocalSQLiteRepository(get_settings().archive_dir)


@contextmanager
def market() -> Iterator[Any]:
    conn = _read_repository().connect(get_settings().market_db)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def lab() -> Iterator[Any]:
    conn = _read_repository().connect(get_settings().lab_db)
    try:
        yield conn
    finally:
        conn.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _as_date(value: str) -> date:
    cleaned = str(value).strip().replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise HTTPException(
            status_code=403, detail="legacy result has no valid OOS range"
        )
    return date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:]))


def _guard_result_range(start: str, end: str, purpose: AccessPurpose) -> str:
    settings = get_settings()
    repository = _read_repository()
    db_paths = [repository.database_path(name) for name in (settings.market_db, settings.lab_db)]
    snapshot_hash = canonical_hash(
        [
            {"name": p.name, "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
            for p in db_paths
        ]
    )
    cfg = load_v2_config()
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=canonical_hash(
            {"protocol": "legacy-read-only-display", "version": 1}
        ),
        snapshot_hash=snapshot_hash,
        start=_as_date(start),
        end=_as_date(end),
        purpose=purpose,
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        raise HTTPException(
            status_code=403,
            detail={
                "decision": receipt.decision.value,
                "receipt_id": receipt.receipt_id,
            },
        )
    return receipt.receipt_id


# ── 概览 / 数据健康的重查询 (仅后台线程执行) ────────────────────────────


def _compute_overview() -> Dict[str, Any]:
    s = get_settings()
    with market() as mk, lab() as lb:
        health = {r["dataset"]: r for r in db.q(mk, "SELECT * FROM data_health")}
        coverage: dict[str, dict[str, Any]] = {
            ds: {"rows": r["rows_total"] or 0, "latest": r["max_date"] or ""}
            for ds, r in health.items()
        }
        # 单因子索引秒回, 避免全表 MAX 扫描
        feat_latest = db.q1(
            lb,
            """SELECT MAX(end_date) mx FROM factor_coverage
                                   WHERE factor_id=(SELECT factor_id FROM factor_registry LIMIT 1)""",
        )
        ev = db.q1(lb, "SELECT COUNT(*) n, MAX(event_date) mx FROM event_store")
        bt = db.q1(lb, "SELECT COUNT(*) n FROM bt_nav")
        coverage["features(因子特征)"] = {
            "rows": 0,
            "latest": feat_latest["mx"] or "",
        }  # rows 单独填
        coverage["events(事件库)"] = {
            "rows": ev["n"] if ev else 0,
            "latest": (ev["mx"] if ev else "") or "",
        }
        coverage["backtest_nav(回测净值)"] = {
            "rows": bt["n"] if bt else 0,
            "latest": "",
        }
        n_factors = db.q1(lb, "SELECT COUNT(*) n FROM factor_registry")["n"]
        n_strats = db.q1(lb, "SELECT COUNT(*) n FROM strategy_registry")["n"]
        # 亿级计数: 只在后台算
        feat_rows = db.q1(lb, "SELECT COALESCE(SUM(row_count), 0) n FROM factor_coverage")["n"]
        coverage["features(因子特征)"]["rows"] = feat_rows
        total_rows = sum(v["rows"] for v in coverage.values())
        latest = max(
            (v["latest"] for v in coverage.values() if v["latest"]), default=""
        )
    return {
        "project": PROJECT_NAME,
        "version": WEBSITE_VERSION,
        "engine": f"qforge {qforge_version}",
        "timestamp": _utc_now(),
        "boundaries": {
            "live_trading": REAL_TRADING_ENABLED,
            "real_order_enabled": REAL_ORDER_ENABLED,
            "local_only": LOCAL_ONLY,
            "broker_enabled": BROKER_ACCESS,
            "account_access": ACCOUNT_ACCESS,
            "broker_auth": BROKER_AUTH,
        },
        "data_source": "LOCAL_ARCHIVE",
        "provenance_status": "LEGACY_EVIDENCE",
        "data_dir": str(s.archive_dir),
        "data_domains": len(coverage),
        "factors": n_factors,
        "strategies": n_strats,
        "total_rows": total_rows,
        "latest_data_date": latest,
        "coverage_summary": coverage,
    }


def _compute_data_health() -> Dict[str, Any]:
    with market() as mk, lab() as lb:
        rows = db.q(mk, "SELECT * FROM data_health ORDER BY dataset")
        coverage: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            coverage[r["dataset"]] = {
                "source_tag": LEGACY_SOURCE_STATUS,
                "total_rows": r["rows_total"],
                "earliest_date": r["min_date"],
                "latest_date": r["max_date"],
                "quality_score": r["quality_score"],
                "status": r["status"],
                "stale_days": r["stale_days"],
            }
        # 去重代码数: 大表全扫 14s/张, 只在后台线程算并缓存
        available_market_tables = {
            str(row["name"])
            for row in db.q(
                mk,
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
        }
        for ds, table in (
            ("bars", "bars"),
            ("etf_daily", "etf_daily"),
            ("bench_daily", "bench_daily"),
            ("norm_daily", "norm_daily"),
            ("norm_daily_basic", "norm_daily_basic"),
        ):
            if ds in coverage and table in available_market_tables:
                coverage[ds]["unique_codes"] = db.q1(
                    mk, f"SELECT COUNT(DISTINCT ts_code) n FROM {table}"
                )["n"]
        if "univ_daily" in coverage and "univ_daily" in available_market_tables:
            coverage["univ_daily"]["unique_codes"] = db.q1(
                mk, "SELECT COUNT(DISTINCT ts_code) n FROM univ_daily"
            )["n"]
        wm_rows = db.q(
            mk,
            "SELECT dataset, key, last_value, updated_at FROM watermark ORDER BY dataset",
        )
        recent_errors = [
            {"api": r["source"], "error": r["message"], "at": r["ts"]}
            for r in db.q(
                lb,
                "SELECT * FROM alerts WHERE level IN ('ERROR','WARN') ORDER BY id DESC LIMIT 20",
            )
        ]
    return {
        "overall_source": LEGACY_SOURCE_STATUS,
        "provenance_status": "LEGACY_UNVERIFIED",
        "coverage": coverage,
        "watermarks": [dict(r) for r in wm_rows],
        "recent_ingestions": len(wm_rows),
        "recent_errors": recent_errors,
    }


# 注册需要后台刷新的重查询 (key 与函数名对应)
_HEAVY_QUERIES: Dict[str, Callable[[], Any]] = {
    "overview": _compute_overview,
    "data_health": _compute_data_health,
}


@app.on_event("startup")
def _start_refresh_loop() -> None:
    # Fail before serving if a permanent safety or path contract was changed.
    cfg = load_v2_config(create_dirs=True)
    assert_safety(cfg.server.host)
    logger.info("V2 canonical API ready; legacy cache prewarm is disabled")


# ── /api/overview ──────────────────────────────────────────────────────


@app.get("/api/overview")
def get_overview() -> Dict[str, Any]:
    """系统概览 (读缓存, 秒回)。"""
    return _cached("overview", _compute_overview)


# ── /api/research/factors ──────────────────────────────────────────────


@app.get("/api/research/factors")
def get_factor_research() -> Dict[str, Any]:
    """因子研究: 注册表 + research 口径统计 (Rank IC / 多空年化)。"""
    with lab() as lb:
        rows = db.q(
            lb,
            """
            SELECT f.factor_id, f.name, f.category, f.status,
                   s.rank_ic_mean, s.rank_ic_ir, s.ic_tstat,
                   s.long_short_ann, s.window_start, s.window_end, s.computed_at
            FROM factor_registry f
            LEFT JOIN factor_stats s ON s.factor_id = f.factor_id AND s.scope='research'
            ORDER BY ABS(COALESCE(s.rank_ic_mean, 0)) DESC
        """,
        )
        factors: List[Dict[str, Any]] = []
        accepted: List[str] = []
        for r in rows:
            if r["window_start"] and r["window_end"]:
                _guard_result_range(
                    r["window_start"], r["window_end"], AccessPurpose.API
                )
            ric = r["rank_ic_mean"]
            tstat = r["ic_tstat"]
            has_stats = ric is not None and tstat is not None
            # 评级规则: |Rank IC|>=0.03 且 |t|>=3 → 可入多因子; 否则仅研究
            rating = (
                "ACCEPT_FOR_MULTIFACTOR"
                if has_stats and abs(ric) >= 0.03 and abs(tstat) >= 3
                else "RESEARCH_ONLY"
            )
            if rating == "ACCEPT_FOR_MULTIFACTOR":
                accepted.append(r["factor_id"])
            factors.append(
                {
                    "name": r["name"] or r["factor_id"],
                    "factor_id": r["factor_id"],
                    "category": r["category"],
                    "rating": rating,
                    "rank_ic": ric,
                    "spread": (r["long_short_ann"] / 100.0)
                    if r["long_short_ann"] is not None
                    else None,
                    "long_short_ann_pct": r["long_short_ann"],
                    "window": f"{r['window_start'] or ''}~{r['window_end'] or ''}"
                    if has_stats
                    else "",
                    "data_source": LEGACY_SOURCE_STATUS,
                    "evaluated_at": r["computed_at"],
                }
            )
    return {"factors": factors, "accepted": accepted, "total_registered": len(factors)}


# ── /api/research/strategies ───────────────────────────────────────────


@app.get("/api/research/strategies")
def get_strategy_research() -> Dict[str, Any]:
    """策略评级: 注册表 + 最新评估 verdict。"""
    with lab() as lb:
        rows = db.q(lb, "SELECT * FROM strategy_registry ORDER BY strategy_id")
        strategies: List[Dict[str, Any]] = []
        for r in rows:
            ev = db.q1(
                lb,
                """
                SELECT verdict, stage, created_at, metrics_json, uses_oos, scope_start, scope_end FROM evaluations
                WHERE strategy_id=? ORDER BY created_at DESC LIMIT 1
            """,
                (r["strategy_id"],),
            )
            paper_acc = db.q1(
                lb,
                "SELECT status FROM paper_account WHERE strategy_id=?",
                (r["strategy_id"],),
            )
            rating = (ev["verdict"] if ev else None) or r["status"]
            if ev and ev["scope_start"] and ev["scope_end"]:
                _guard_result_range(
                    ev["scope_start"], ev["scope_end"], AccessPurpose.API
                )
            strategies.append(
                {
                    "name": r["name"] or r["strategy_id"],
                    "strategy_id": r["strategy_id"],
                    "category": r["category"],
                    "rating": rating,
                    "registry_status": r["status"],
                    "has_paper_account": paper_acc is not None,
                    "data_source": LEGACY_SOURCE_STATUS,
                    "last_evaluated": ev["created_at"] if ev else "",
                    "promotion_history": [],
                }
            )
        paper_ids = [
            r["strategy_id"] for r in db.q(lb, "SELECT strategy_id FROM paper_account")
        ]
    return {"strategies": strategies, "paper_eligible": paper_ids}


# ── /api/research/backtests ────────────────────────────────────────────


@app.get("/api/research/backtests")
def get_backtests() -> Dict[str, Any]:
    """回测列表: 每次回测的元信息与收益摘要 (净值归一为 1.0 起点)。"""
    with lab() as lb:
        rows = db.q(
            lb,
            """
            SELECT b.bt_id, b.strategy_id, b.start, b.end, b.created_at,
                   b.engine, b.uses_oos, b.note,
                   COUNT(n.trade_date) AS n_days,
                   n1.nav AS first_nav, n1.bench_nav AS first_bench,
                   n2.nav AS final_nav, n2.bench_nav AS final_bench
            FROM backtests b
            JOIN bt_nav n ON n.bt_id = b.bt_id
            JOIN bt_nav n1 ON n1.bt_id = b.bt_id AND n1.trade_date = (
                SELECT MIN(trade_date) FROM bt_nav WHERE bt_id = b.bt_id)
            JOIN bt_nav n2 ON n2.bt_id = b.bt_id AND n2.trade_date = (
                SELECT MAX(trade_date) FROM bt_nav WHERE bt_id = b.bt_id)
            GROUP BY b.bt_id
            ORDER BY b.created_at DESC
        """,
        )
        out = []
        for r in rows:
            _guard_result_range(r["start"], r["end"], AccessPurpose.API)
            sname = db.q1(
                lb,
                "SELECT name FROM strategy_registry WHERE strategy_id=?",
                (r["strategy_id"],),
            )
            out.append(
                {
                    "bt_id": r["bt_id"],
                    "strategy_id": r["strategy_id"],
                    "strategy_name": sname["name"] if sname else r["strategy_id"],
                    "start": r["start"],
                    "end": r["end"],
                    "created_at": r["created_at"],
                    "engine": r["engine"],
                    "uses_oos": bool(r["uses_oos"]),
                    "n_days": r["n_days"],
                    "total_return": (r["final_nav"] / r["first_nav"] - 1.0)
                    if r["first_nav"]
                    else None,
                    "bench_return": (r["final_bench"] / r["first_bench"] - 1.0)
                    if r["final_bench"] and r["first_bench"]
                    else None,
                }
            )
    return {"backtests": out}


@app.get("/api/research/backtests/{bt_id}/nav")
def get_backtest_nav(bt_id: str) -> Any:
    """单次回测的净值曲线 (策略/基准均归一为 1.0 起点) + 绩效摘要。"""
    with lab() as lb:
        meta = db.q1(
            lb,
            """
            SELECT b.*, s.name AS strategy_name FROM backtests b
            LEFT JOIN strategy_registry s ON s.strategy_id = b.strategy_id
            WHERE b.bt_id=?
        """,
            (bt_id,),
        )
        if not meta:
            return JSONResponse({"error": "backtest not found"}, status_code=404)
        receipt_id = _guard_result_range(
            meta["start"], meta["end"], AccessPurpose.CHART
        )
        rows = db.q(
            lb,
            """
            SELECT trade_date, nav, ret, bench_nav, bench_ret, exposure
            FROM bt_nav WHERE bt_id=? ORDER BY trade_date
        """,
            (bt_id,),
        )

        # 归一: 策略净值以首日起始值为 1.0, 基准同理, 保证同图可比
        nav0 = next((r["nav"] for r in rows if r["nav"]), None)
        bench0 = next((r["bench_nav"] for r in rows if r["bench_nav"]), None)
        series = [
            {
                "trade_date": r["trade_date"],
                "nav": (r["nav"] / nav0) if (r["nav"] and nav0) else None,
                "bench_nav": (r["bench_nav"] / bench0)
                if (r["bench_nav"] and bench0)
                else None,
                "exposure": r["exposure"],
            }
            for r in rows
        ]

        # 绩效摘要 (基于原始净值比例)
        navs = [r["nav"] for r in rows if r["nav"] is not None]
        summary: Dict[str, Any] = {}
        if len(navs) >= 2:
            total_ret = navs[-1] / navs[0] - 1.0
            years = max(len(navs) / 244.0, 1e-9)
            ann_ret = (navs[-1] / navs[0]) ** (1.0 / years) - 1.0
            peak, max_dd = navs[0], 0.0
            for v in navs:
                peak = max(peak, v)
                max_dd = max(max_dd, 1.0 - v / peak)
            rets = [r["ret"] for r in rows if r["ret"] is not None]
            mean = sum(rets) / len(rets) if rets else 0.0
            var = (
                sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
                if len(rets) > 1
                else 0.0
            )
            std = var**0.5
            sharpe = (mean / std * (244**0.5)) if std > 0 else None
            bench = [r["bench_nav"] for r in rows if r["bench_nav"] is not None]
            summary = {
                "total_return": total_ret,
                "ann_return": ann_ret,
                "max_drawdown": max_dd,
                "sharpe": sharpe,
                "n_days": len(navs),
                "bench_total_return": (bench[-1] / bench[0] - 1.0)
                if len(bench) >= 2
                else None,
            }
        return {
            "bt_id": bt_id,
            "strategy_id": meta["strategy_id"],
            "strategy_name": meta["strategy_name"] or meta["strategy_id"],
            "start": meta["start"],
            "end": meta["end"],
            "created_at": meta["created_at"],
            "series": series,
            "summary": summary,
            "access_receipt_id": receipt_id,
        }


# ── /api/research-queue ────────────────────────────────────────────────


@app.get("/api/research-queue")
def get_research_queue() -> Dict[str, Any]:
    with lab() as lb:
        rows = db.q(
            lb,
            """
            SELECT item_id, kind, target, priority, status, reason, created_at, updated_at
            FROM research_queue ORDER BY status, priority, created_at LIMIT 100
        """,
        )
        return {"queue": [dict(r) for r in rows]}


# ── /api/market/daily ──────────────────────────────────────────────────


@app.get("/api/market/daily")
def get_market_daily(
    ts_code: str = Query("510300.SH"),
    start_date: str = Query(""),
    end_date: str = Query(""),
) -> Dict[str, Any]:
    """日线 OHLCV。查找顺序: etf_daily(基金) → bars(个股) → bench_daily(指数)。"""
    conds, params = ["ts_code=?"], [ts_code]
    if start_date:
        conds.append("trade_date>=?")
        params.append(start_date)
    if end_date:
        conds.append("trade_date<=?")
        params.append(end_date)
    where = " AND ".join(conds)
    cols = "ts_code, trade_date, open, high, low, close, pre_close, vol, amount"

    with market() as mk:
        for table in ("etf_daily", "bars", "bench_daily"):
            rows = db.q(
                mk,
                f"SELECT {cols} FROM {table} WHERE {where} ORDER BY trade_date",
                tuple(params),
            )
            if rows:
                data = [dict(r) for r in rows]
                receipt_id = _guard_result_range(
                    data[0]["trade_date"], data[-1]["trade_date"], AccessPurpose.CHART
                )
                return {
                    "data": data,
                    "count": len(data),
                    "ts_code": ts_code,
                    "source_table": table,
                    "access_receipt_id": receipt_id,
                }
    return {"data": [], "count": 0, "ts_code": ts_code, "source_table": None}


@app.get("/api/market/securities")
def get_securities(
    q: str = Query("", max_length=20),
    limit: int = Query(200, le=1000),
) -> Dict[str, Any]:
    """证券列表/搜索: 支持代码或名称模糊匹配 (股票 + ETF + 指数)。"""
    with market() as mk:
        like = f"%{q.strip()}%"
        stocks = db.q(
            mk,
            """
            SELECT ts_code, name, exchange, board, sec_type, list_date
            FROM sec_master
            WHERE (ts_code LIKE ? OR symbol LIKE ? OR name LIKE ?) AND list_status='L'
            ORDER BY ts_code LIMIT ?
        """,
            (like, like, like, limit),
        )
        etfs = db.q(
            mk,
            """
            SELECT b.ts_code, b.name, 'SH/SZ' AS exchange, 'ETF' AS board, '基金' AS sec_type, b.list_date
            FROM norm_fund_basic b
            WHERE b.ts_code IN (SELECT DISTINCT ts_code FROM etf_daily)
              AND (b.ts_code LIKE ? OR b.name LIKE ?)
            ORDER BY b.ts_code LIMIT 100
        """,
            (like, like),
        )
        indexes = db.q(
            mk,
            """
            SELECT DISTINCT ts_code FROM bench_daily WHERE ts_code LIKE ?
            ORDER BY ts_code LIMIT 20
        """,
            (like,),
        )
        data = (
            [dict(r) for r in stocks]
            + [dict(r) for r in etfs]
            + [
                {
                    "ts_code": r["ts_code"],
                    "name": r["ts_code"],
                    "exchange": "SH/SZ",
                    "board": "指数",
                    "sec_type": "INDEX",
                    "list_date": None,
                }
                for r in indexes
            ]
        )
        total = db.q1(mk, "SELECT COUNT(*) n FROM sec_master WHERE list_status='L'")[
            "n"
        ]
    return {"data": data, "count": total, "shown": len(data), "query": q}


# ── /api/paper/portfolio ───────────────────────────────────────────────


@app.get("/api/paper/portfolio")
def get_paper_portfolio() -> Dict[str, Any]:
    """PAPER 模拟盘: 账户、持仓、净值序列。"""
    with lab() as lb, market() as mk:
        accounts = db.q(lb, "SELECT * FROM paper_account ORDER BY strategy_id")
        # 名称映射在 market 库, 持仓在 lab 库, 分开查询后合并 (SQLite 跨库不 join)
        names = {
            r["ts_code"]: r["name"]
            for r in db.q(mk, "SELECT ts_code, name FROM sec_master")
        }
        out: List[Dict[str, Any]] = []
        for a in accounts:
            sid = a["strategy_id"]
            positions = db.q(
                lb,
                """
                SELECT * FROM paper_positions
                WHERE strategy_id=? AND shares > 0
                ORDER BY shares * last_price DESC
            """,
                (sid,),
            )
            nav_rows = db.q(
                lb,
                """
                SELECT trade_date, nav, cash, positions_value, benchmark_nav
                FROM paper_nav WHERE strategy_id=? ORDER BY trade_date
            """,
                (sid,),
            )
            last_nav = nav_rows[-1] if nav_rows else None
            receipt_id = (
                _guard_result_range(
                    nav_rows[0]["trade_date"],
                    nav_rows[-1]["trade_date"],
                    AccessPurpose.API,
                )
                if nav_rows
                else None
            )
            nav_series = [
                {
                    "trade_date": r["trade_date"],
                    "nav": r["nav"],
                    "cash": r["cash"],
                    "positions_value": r["positions_value"],
                }
                for r in nav_rows[-260:]
            ]
            total_ret = (
                (a["nav"] / a["initial_cash"] - 1.0) if a["initial_cash"] else None
            )
            out.append(
                {
                    "strategy_id": sid,
                    "status": a["status"],
                    "cash": a["cash"],
                    "nav": a["nav"],
                    "initial_cash": a["initial_cash"],
                    "total_return": total_ret,
                    "updated_at": a["updated_at"],
                    "started_at": a["started_at"],
                    "last_trade_date": last_nav["trade_date"] if last_nav else None,
                    "positions": [
                        {**dict(p), "name": names.get(p["ts_code"])} for p in positions
                    ],
                    "nav_series": nav_series,
                    "access_receipt_id": receipt_id,
                }
            )
    return {
        "message": "PAPER portfolio tracker",
        "paper_eligible_only": True,
        "live_trading": False,
        "accounts": out,
    }


# ── /api/events ────────────────────────────────────────────────────────


@app.get("/api/events")
def get_events(
    limit: int = Query(50, le=500),
    kind: str = Query(""),
) -> Dict[str, Any]:
    """事件情报: 支持按类型过滤; 附带各类型的效果统计 (CAR/胜率)。"""
    with lab() as lb:
        conds, params = ["1=1"], []
        if kind:
            conds.append("e.kind=?")
            params.append(kind)
        where = " AND ".join(conds)
        rows = db.q(
            lb,
            f"""
            SELECT e.event_id, e.ts_code, e.company_name, e.kind, e.score,
                   e.event_ts, e.event_date, e.source, e.title
            FROM event_store e WHERE {where}
            ORDER BY e.event_date DESC, e.event_id LIMIT ?
        """,
            (*params, limit),
        )
        total = db.q1(lb, "SELECT COUNT(*) n FROM event_store")["n"]
        events = []
        event_receipt_id = (
            _guard_result_range(
                rows[-1]["event_date"], rows[0]["event_date"], AccessPurpose.EVENT
            )
            if rows
            else None
        )
        for r in rows:
            events.append(
                {
                    "event_id": r["event_id"],
                    "ts_code": r["ts_code"],
                    "company_name": r["company_name"],
                    "event_type": r["kind"],
                    "score": r["score"],
                    "published_at": r["event_date"],
                    "title": r["title"],
                    "summary": f"{r['company_name'] or r['ts_code'] or ''} · {r['kind']}",
                    "source": r["source"],
                }
            )
        # 各类型: 事件数 + 研究口径 CAR/胜率 (event_stats 由研究流水线写入)
        stats_map = {
            r["kind"]: r
            for r in db.q(lb, "SELECT * FROM event_stats WHERE scope='research'")
        }
        kind_counts = {
            r["kind"]: r["n"]
            for r in db.q(
                lb,
                "SELECT kind, COUNT(*) n FROM event_store GROUP BY kind ORDER BY n DESC",
            )
        }
        kind_stats = []
        for k, n in kind_counts.items():
            st = stats_map.get(k)
            kind_stats.append(
                {
                    "kind": k,
                    "count": n,
                    "car_5": st["car_5"] if st else None,
                    "car_10": st["car_10"] if st else None,
                    "car_20": st["car_20"] if st else None,
                    "hit_rate": st["hit_rate"] if st else None,
                }
            )
    return {
        "events": events,
        "count": total,
        "kind_stats": kind_stats,
        "access_receipt_id": event_receipt_id,
    }


# ── /api/data-health ───────────────────────────────────────────────────


@app.get("/api/data-health")
def get_data_health() -> Dict[str, Any]:
    """数据健康 (读缓存, 秒回; 含去重代码数)。"""
    return _cached("data_health", _compute_data_health)


# ── /api/system ────────────────────────────────────────────────────────


@app.get("/api/system")
def get_system_status() -> Dict[str, Any]:
    """系统状态: 运行台账 (run_ledger) 与安全边界。"""
    with lab() as lb:
        rows = db.q(
            lb,
            """
            SELECT run_id, job, business_date, status, started_at, finished_at, error
            FROM run_ledger ORDER BY started_at DESC LIMIT 20
        """,
        )
        runs = []
        for r in rows:
            st, ft = _parse_ts(r["started_at"]), _parse_ts(r["finished_at"])
            duration = round((ft - st).total_seconds(), 1) if st and ft else None
            runs.append(
                {
                    "job": r["job"],
                    "status": r["status"],
                    "duration": duration,
                    "at": r["started_at"],
                    "business_date": r["business_date"],
                    "error": r["error"],
                }
            )
    return {
        "project": PROJECT_NAME,
        "version": WEBSITE_VERSION,
        "engine": f"qforge {qforge_version}",
        "local_only": LOCAL_ONLY,
        "live_trading": REAL_TRADING_ENABLED,
        "real_order_enabled": REAL_ORDER_ENABLED,
        "broker_access": BROKER_ACCESS,
        "account_access": ACCOUNT_ACCESS,
        "broker_auth": BROKER_AUTH,
        "recent_runs": runs,
    }


# ── 前端静态托管 (dashboard/dist) ───────────────────────────────────────

FRONTEND_DIST = ROOT / "dashboard" / "dist"


@app.get("/", include_in_schema=False)
def serve_root():
    return _serve_spa(Path("index.html"))


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    """SPA 回退: 静态资源直出, 其余路径回退到 index.html (React Router)。"""
    if full_path.startswith("api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    candidate = FRONTEND_DIST / full_path
    if (
        full_path
        and candidate.is_file()
        and candidate.parent.is_relative_to(FRONTEND_DIST)
    ):
        return FileResponse(candidate)
    return _serve_spa(Path(full_path))


def _serve_spa(_path: Path) -> Any:
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse(
        "<h3>前端尚未构建</h3><p>请先在 dashboard/ 目录执行 <code>npm install && npm run build</code>。</p>"
        "<p>API 文档: <a href='/docs'>/docs</a></p>",
        status_code=200,
    )


if FRONTEND_DIST.exists() and (FRONTEND_DIST / "assets").exists():
    app.mount(
        "/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets"
    )


def start_server() -> None:
    """启动网站 (LOCAL ONLY)。用法: python -m quantweb.app"""
    import uvicorn

    s = get_settings()
    host = getattr(s, "dashboard_host", "127.0.0.1")
    port = int(getattr(s, "dashboard_port", 8888) or 8888)
    assert_safety(host)
    load_v2_config(create_dirs=True)
    logger.info(
        "量化分析网站启动: http://%s:%d (local evidence archive: %s)",
        host,
        port,
        s.archive_dir,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_server()
