# -*- coding: utf-8 -*-
"""SQLite storage layer.

Three databases (inside data/):
  raw.db     -- immutable vendor payloads (triggers forbid UPDATE/DELETE)
  market.db  -- normalized + curated market data + sync metadata
  lab.db     -- features, research, backtests, OOS registry, paper ledger, runtime

All connections use WAL + busy_timeout; writes are idempotent upserts.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings
from .util import json_dumps, json_loads  # re-exported: db.json_dumps used platform-wide
from src.quantcore.contracts.safety import PathBoundary

__all__ = ["json_dumps", "json_loads", "connect", "connect_raw", "connect_market",
           "connect_lab", "tx", "upsert", "q", "q1", "kv_get", "kv_set",
           "wm_get", "wm_set"]

RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_meta (
  batch_id    TEXT PRIMARY KEY,
  api         TEXT NOT NULL,
  source      TEXT NOT NULL,
  params_json TEXT NOT NULL,
  request_id  TEXT,
  fetched_at  TEXT NOT NULL,
  row_count   INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS raw_rows (
  rid          INTEGER PRIMARY KEY AUTOINCREMENT,
  api          TEXT NOT NULL,
  batch_id     TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  ts_code      TEXT,
  trade_date   TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_rows_api_date ON raw_rows(api, trade_date);
CREATE INDEX IF NOT EXISTS ix_raw_rows_api_code ON raw_rows(api, ts_code);
CREATE TRIGGER IF NOT EXISTS trg_raw_rows_immutable_update
  BEFORE UPDATE ON raw_rows BEGIN
    SELECT RAISE(ABORT, 'RAW LAYER IS IMMUTABLE (update forbidden)');
  END;
CREATE TRIGGER IF NOT EXISTS trg_raw_rows_immutable_delete
  BEFORE DELETE ON raw_rows BEGIN
    SELECT RAISE(ABORT, 'RAW LAYER IS IMMUTABLE (delete forbidden)');
  END;
CREATE TRIGGER IF NOT EXISTS trg_raw_meta_immutable_update
  BEFORE UPDATE ON raw_meta BEGIN
    SELECT RAISE(ABORT, 'RAW LAYER IS IMMUTABLE (update forbidden)');
  END;
CREATE TRIGGER IF NOT EXISTS trg_raw_meta_immutable_delete
  BEFORE DELETE ON raw_meta BEGIN
    SELECT RAISE(ABORT, 'RAW LAYER IS IMMUTABLE (delete forbidden)');
  END;
"""

MARKET_DDL = """
-- ---------- normalized (derived from raw, replaceable) ----------
CREATE TABLE IF NOT EXISTS norm_trade_cal (
  exchange TEXT NOT NULL, cal_date TEXT NOT NULL, is_open INTEGER NOT NULL,
  pretrade_date TEXT, PRIMARY KEY (exchange, cal_date));
CREATE TABLE IF NOT EXISTS norm_stock_basic (
  ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT, industry TEXT,
  market TEXT, exchange TEXT, list_status TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE IF NOT EXISTS norm_namechange (
  ts_code TEXT NOT NULL, name TEXT, start_date TEXT, end_date TEXT,
  ann_date TEXT, change_reason TEXT, PRIMARY KEY (ts_code, start_date, name));
CREATE TABLE IF NOT EXISTS norm_suspend (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, suspend_type TEXT,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_daily (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL,
  change REAL, pct_chg REAL, vol REAL, amount REAL, adj_factor REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_daily_basic (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  close REAL, turnover_rate REAL, turnover_rate_f REAL, volume_ratio REAL,
  pe REAL, pe_ttm REAL, pb REAL, ps REAL, ps_ttm REAL, dv_ratio REAL, dv_ttm REAL,
  total_share REAL, float_share REAL, free_share REAL, total_mv REAL, circ_mv REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_stk_limit (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  pre_close REAL, up_limit REAL, down_limit REAL, PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_index_daily (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL, vol REAL, amount REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_fund_basic (
  ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, fund_type TEXT, found_date TEXT,
  list_date TEXT, delist_date TEXT, management TEXT, status TEXT, market TEXT);
CREATE TABLE IF NOT EXISTS norm_fund_daily (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL, vol REAL, amount REAL,
  adj_factor REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_fund_adj (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, adj_factor REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS norm_fina_indicator (
  ts_code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT, f_ann_date TEXT,
  roe REAL, roe_dt REAL, grossprofit_margin REAL, netprofit_margin REAL,
  debt_to_assets REAL, ocf_to_or REAL, eps REAL, bps REAL, or_yoy REAL, netprofit_yoy REAL,
  extra_json TEXT, PRIMARY KEY (ts_code, end_date));
CREATE TABLE IF NOT EXISTS norm_dividend (
  ts_code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT, div_proc TEXT,
  stk_div TEXT, stk_bo_rate REAL, stk_co_rate REAL, cash_div REAL, cash_div_tax REAL,
  base_share REAL, ex_date TEXT, pay_date TEXT, PRIMARY KEY (ts_code, end_date, div_proc));
CREATE TABLE IF NOT EXISTS norm_anns (
  ann_id TEXT, ts_code TEXT, name TEXT, ann_date TEXT, title TEXT,
  PRIMARY KEY (ann_id));
CREATE TABLE IF NOT EXISTS norm_news (
  nid TEXT, datetime TEXT, src TEXT, title TEXT, content TEXT,
  PRIMARY KEY (nid));

-- ---------- curated (PIT, engine-facing) ----------
CREATE TABLE IF NOT EXISTS cal (
  trade_date TEXT PRIMARY KEY, is_open INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sec_master (
  ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, exchange TEXT,
  board TEXT, sec_type TEXT, list_date TEXT, delist_date TEXT, list_status TEXT);
CREATE TABLE IF NOT EXISTS name_hist (
  ts_code TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT, name TEXT,
  is_st INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ts_code, start_date));
CREATE TABLE IF NOT EXISTS univ_daily (
  trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,
  board TEXT, sec_type TEXT,
  is_listed INTEGER NOT NULL, is_suspended INTEGER NOT NULL DEFAULT 0,
  is_st INTEGER NOT NULL DEFAULT 0, is_new INTEGER NOT NULL DEFAULT 0,
  listed_days INTEGER, eligible_research INTEGER NOT NULL DEFAULT 0,
  eligible_production INTEGER NOT NULL DEFAULT 0,
  reasons TEXT,
  PRIMARY KEY (trade_date, ts_code));
CREATE TABLE IF NOT EXISTS corp_action (
  ts_code TEXT NOT NULL, ex_date TEXT NOT NULL,
  cash_div REAL DEFAULT 0, stk_ratio REAL DEFAULT 0,
  adj_before REAL, adj_after REAL, source TEXT,
  PRIMARY KEY (ts_code, ex_date));
CREATE TABLE IF NOT EXISTS bars (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL,
  vol REAL, amount REAL, adj_factor REAL,
  up_limit REAL, down_limit REAL, limit_source TEXT,
  is_suspended INTEGER DEFAULT 0, is_st INTEGER DEFAULT 0, board TEXT,
  PRIMARY KEY (ts_code, trade_date));
CREATE INDEX IF NOT EXISTS ix_bars_date ON bars(trade_date);
CREATE TABLE IF NOT EXISTS bench_daily (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL, vol REAL, amount REAL,
  PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS etf_daily (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, pre_close REAL, vol REAL, amount REAL,
  adj_factor REAL, PRIMARY KEY (ts_code, trade_date));
CREATE TABLE IF NOT EXISTS data_health (
  dataset TEXT NOT NULL, checked_at TEXT NOT NULL,
  rows_total INTEGER, min_date TEXT, max_date TEXT, stale_days INTEGER,
  null_ratio REAL, dup_ratio REAL, quality_score REAL, status TEXT, detail TEXT,
  PRIMARY KEY (dataset));

-- ---------- meta ----------
CREATE TABLE IF NOT EXISTS watermark (
  dataset TEXT NOT NULL, key TEXT NOT NULL, last_value TEXT,
  updated_at TEXT, PRIMARY KEY (dataset, key));
CREATE TABLE IF NOT EXISTS meta_kv (
  k TEXT PRIMARY KEY, v TEXT);
"""

LAB_DDL = """
-- ---------- factor registry & features ----------
CREATE TABLE IF NOT EXISTS factor_registry (
  factor_id TEXT PRIMARY KEY, name TEXT, category TEXT, description TEXT,
  params_json TEXT, version TEXT, created_at TEXT, status TEXT DEFAULT 'ACTIVE');
CREATE TABLE IF NOT EXISTS features (
  factor_id TEXT NOT NULL, ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  value REAL, available_at TEXT NOT NULL,
  PRIMARY KEY (factor_id, ts_code, trade_date));
CREATE INDEX IF NOT EXISTS ix_features_date ON features(factor_id, trade_date);
CREATE TABLE IF NOT EXISTS factor_stats (
  factor_id TEXT NOT NULL, scope TEXT NOT NULL, window_start TEXT, window_end TEXT,
  ic_mean REAL, ic_ir REAL, rank_ic_mean REAL, rank_ic_ir REAL, ic_tstat REAL,
  turnover REAL, long_short_ann REAL, monotonics REAL, computed_at TEXT,
  stats_json TEXT, PRIMARY KEY (factor_id, scope));
CREATE TABLE IF NOT EXISTS factor_ic_daily (
  factor_id TEXT NOT NULL, scope TEXT NOT NULL, trade_date TEXT NOT NULL,
  ic REAL, PRIMARY KEY (factor_id, scope, trade_date));

-- ---------- strategy registry & research ----------
CREATE TABLE IF NOT EXISTS strategy_registry (
  strategy_id TEXT PRIMARY KEY, name TEXT, description TEXT, category TEXT,
  params_json TEXT, version TEXT, created_at TEXT, status TEXT DEFAULT 'RESEARCH');
CREATE TABLE IF NOT EXISTS research_queue (
  item_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
  priority INTEGER DEFAULT 5, status TEXT DEFAULT 'PENDING',
  reason TEXT, created_at TEXT, updated_at TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS backtests (
  bt_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, params_hash TEXT,
  params_json TEXT, start TEXT, end TEXT, scope TEXT,
  created_at TEXT, engine TEXT, engine_version TEXT, uses_oos INTEGER DEFAULT 0,
  seed INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS bt_nav (
  bt_id TEXT NOT NULL, trade_date TEXT NOT NULL, nav REAL, ret REAL,
  bench_nav REAL, bench_ret REAL, exposure REAL, PRIMARY KEY (bt_id, trade_date));
CREATE TABLE IF NOT EXISTS bt_trades (
  bt_id TEXT NOT NULL, seq INTEGER NOT NULL, trade_date TEXT, ts_code TEXT,
  side TEXT, shares REAL, price REAL, value REAL, fee REAL, cash_after REAL, note TEXT,
  PRIMARY KEY (bt_id, seq));
CREATE TABLE IF NOT EXISTS bt_orders (
  bt_id TEXT NOT NULL, seq INTEGER NOT NULL, issued_date TEXT, exec_date TEXT,
  ts_code TEXT, side TEXT, shares REAL, target_weight REAL, value_delta REAL,
  status TEXT, age INTEGER, note TEXT,
  PRIMARY KEY (bt_id, seq));
CREATE TABLE IF NOT EXISTS evaluations (
  eval_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, stage TEXT NOT NULL,
  scope_start TEXT, scope_end TEXT, verdict TEXT NOT NULL,
  metrics_json TEXT, gates_json TEXT, uses_oos INTEGER DEFAULT 0,
  engine_version TEXT, created_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS oos_registry (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'LOCKED', consumed_at TEXT, consumed_by TEXT, note TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS validation_runs (
  v_id TEXT PRIMARY KEY, bt_id TEXT NOT NULL, verdict TEXT NOT NULL,
  nav_max_rel_diff REAL, nav_mean_abs_diff REAL, trades_compared INTEGER,
  detail_json TEXT, created_at TEXT);

-- ---------- paper ledger ----------
CREATE TABLE IF NOT EXISTS paper_account (
  strategy_id TEXT PRIMARY KEY, cash REAL NOT NULL, nav REAL NOT NULL,
  initial_cash REAL NOT NULL, updated_at TEXT, status TEXT DEFAULT 'ACTIVE',
  started_at TEXT);
CREATE TABLE IF NOT EXISTS paper_positions (
  strategy_id TEXT NOT NULL, ts_code TEXT NOT NULL,
  shares REAL NOT NULL, avg_cost REAL NOT NULL, available_shares REAL NOT NULL DEFAULT 0,
  last_price REAL, updated_at TEXT, PRIMARY KEY (strategy_id, ts_code));
CREATE TABLE IF NOT EXISTS paper_signals (
  sig_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, signal_date TEXT NOT NULL,
  payload_json TEXT NOT NULL, generated_at TEXT, status TEXT DEFAULT 'PENDING');
CREATE TABLE IF NOT EXISTS paper_orders (
  order_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, signal_date TEXT,
  order_date TEXT NOT NULL, ts_code TEXT NOT NULL, side TEXT NOT NULL,
  shares INTEGER NOT NULL, status TEXT DEFAULT 'PENDING',
  created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS paper_fills (
  fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, ts_code TEXT NOT NULL, side TEXT NOT NULL,
  shares INTEGER NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
  cash_after REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS paper_nav (
  strategy_id TEXT NOT NULL, trade_date TEXT NOT NULL, nav REAL NOT NULL,
  cash REAL NOT NULL, positions_value REAL NOT NULL, benchmark_nav REAL,
  PRIMARY KEY (strategy_id, trade_date));
CREATE TABLE IF NOT EXISTS paper_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT, ts TEXT,
  level TEXT, message TEXT);

-- ---------- event intelligence ----------
CREATE TABLE IF NOT EXISTS event_store (
  event_id TEXT PRIMARY KEY, ts_code TEXT, company_name TEXT,
  kind TEXT NOT NULL, score REAL NOT NULL,
  event_ts TEXT NOT NULL, event_date TEXT NOT NULL,
  source TEXT NOT NULL, title TEXT, ref_id TEXT,
  created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_event_store_date ON event_store(event_date);
CREATE INDEX IF NOT EXISTS ix_event_store_code ON event_store(ts_code);
CREATE TABLE IF NOT EXISTS event_stats (
  kind TEXT NOT NULL, scope TEXT NOT NULL,
  n_events INTEGER, car_1 REAL, car_5 REAL, car_10 REAL, car_20 REAL,
  hit_rate REAL, computed_at TEXT, detail_json TEXT,
  PRIMARY KEY (kind, scope));

-- ---------- runtime ----------
CREATE TABLE IF NOT EXISTS run_ledger (
  run_id TEXT PRIMARY KEY, job TEXT NOT NULL, business_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER DEFAULT 0,
  started_at TEXT, finished_at TEXT, heartbeat_at TEXT,
  checkpoint_json TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_run_ledger_job ON run_ledger(job, business_date);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, source TEXT, message TEXT,
  ack INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: str | Path, ddl: str = "") -> sqlite3.Connection:
    # This is a write-capable legacy connection. It may never target a legacy
    # project or any location outside the main Integrated V2 project.
    p = PathBoundary().require_writable(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA cache_size=-32000")
    if ddl:
        conn.executescript(ddl)
    return conn


def connect_raw() -> sqlite3.Connection:
    s = get_settings()
    return connect(s.db_path(s.raw_db), RAW_DDL)


def connect_market() -> sqlite3.Connection:
    s = get_settings()
    return connect(s.db_path(s.market_db), MARKET_DDL)


def connect_lab() -> sqlite3.Connection:
    s = get_settings()
    return connect(s.db_path(s.lab_db), LAB_DDL)


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], cols: list[str],
           batch: int = 500) -> int:
    """INSERT OR REPLACE rows (dicts) restricted to cols. Returns rowcount."""
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    n = 0
    with tx(conn):
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in chunk])
            n += len(chunk)
    return n


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def q1(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def kv_get(conn: sqlite3.Connection, k: str, default=None):
    row = q1(conn, "SELECT v FROM meta_kv WHERE k=?", (k,))
    return row["v"] if row else default


def kv_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    upsert(conn, "meta_kv", [{"k": k, "v": v}], ["k", "v"])


def wm_get(conn: sqlite3.Connection, dataset: str, key: str = "default") -> str | None:
    row = q1(conn, "SELECT last_value FROM watermark WHERE dataset=? AND key=?", (dataset, key))
    return row["last_value"] if row else None


def wm_set(conn: sqlite3.Connection, dataset: str, key: str, value: str) -> None:
    from .util import now_ts
    upsert(conn, "watermark", [{"dataset": dataset, "key": key, "last_value": value,
                                "updated_at": now_ts()}], ["dataset", "key", "last_value", "updated_at"])
