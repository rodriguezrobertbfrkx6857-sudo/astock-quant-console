# -*- coding: utf-8 -*-
"""Legacy-compatible configuration behind the Integrated V2 safety boundary.

Precedence: env var (QF_*) > config/settings.local.json > config/settings.json > defaults.

All writable paths are constrained to the main project. Historical evidence is
served from the published local archive; external projects are handled only by
offline migration tooling.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from src.quantcore.contracts.safety import (
    PathBoundary,
    validate_runtime_safety,
    validate_safety_overrides,
)

ROOT = Path(__file__).resolve().parents[1]

def assert_safety(host: str | None = None) -> None:
    """Runtime check that remains active under ``python -O``."""
    validate_runtime_safety(host=host)


@dataclass
class Settings:
    # --- storage ---
    data_dir: str = str(ROOT / "data")
    logs_dir: str = str(ROOT / "logs")
    raw_db: str = "raw.db"          # inside data_dir; immutable RAW layer
    market_db: str = "market.db"    # normalized + curated + meta
    lab_db: str = "lab.db"          # features + research + paper + runtime ledger
    archive_dir: str = str(ROOT / "data" / "v2" / "legacy_archive" / "published")

    # --- vendor relays ---
    datahubco_base: str = "https://data.example.invalid/tushare"
    promax_base: str = "https://relay.example.invalid/tushare/pro"
    datahubco_enabled: bool = False
    promax_enabled: bool = False
    # client-side pacing (requests/second) to stay far below IP limits
    datahubco_min_interval: float = 0.20
    promax_min_interval: float = 0.35   # <= ~170 req/min vs 200/min IP cap
    vendor_timeout: float = 30.0
    vendor_max_retries: int = 3         # 429/502/503/504 backoff retries
    vendor_backoff_base: float = 1.5
    breaker_fail_threshold: int = 5
    breaker_cooldown_sec: float = 300.0
    verify_ssl: bool = True

    # --- data scope ---
    history_start: str = "20180101"     # inclusive YYYYMMDD
    calendar_end: str = "20301231"
    benchmark_codes: tuple = ("000001.SH", "000300.SH", "000905.SH", "000852.SH", "399006.SZ")
    etf_pool: tuple = (
        "510300.SH",  # 沪深300ETF
        "510500.SH",  # 中证500ETF
        "512100.SH",  # 中证1000ETF
        "159915.SZ",  # 创业板ETF
        "512880.SH",  # 证券ETF
        "512010.SH",  # 医药ETF
        "515180.SH",  # 科创板? (515180 易方达中证科创创业50) -- pool is data-driven anyway
        "518880.SH",  # 黄金ETF
        "513100.SH",  # 纳指ETF
        "513050.SH",  # 中概互联ETF
        "511010.SH",  # 国债ETF
    )
    # optional datasets (fetched only if the relay authorizes; pipelines degrade gracefully)
    fetch_daily_basic: bool = True
    fetch_stk_limit: bool = True
    fetch_suspend: bool = True
    fetch_namechange: bool = True
    fetch_fundamentals: bool = False    # heavy: per-stock fina_indicator
    fetch_events_window_days: int = 90  # announcements/news lookback for Event Intelligence

    # --- PIT policy ---
    bar_available_hour: int = 17        # bars treated as knowable at T 17:00 (vendor updates 15-17)
    fund_lag_days: int = 1              # fundamentals usable from ann_date + 1 day 00:00
    event_lag_minutes: int = 5          # news usable at publish ts + 5min

    # --- backtest defaults ---
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_tax_sell: float = 0.0005      # 2023-08-28 后; engine time-varyes to 0.001 before
    transfer_fee: float = 0.00001       # SH both sides (approximation, see docs)
    slippage_bps: float = 10.0
    lot_size: int = 100
    max_position_weight: float = 0.15
    max_positions: int = 50
    min_trade_value: float = 1000.0
    rebalance_band: float = 0.005       # skip trades when |delta w| < band
    new_stock_min_days: int = 60        # production-eligible requires listed >= 60d
    delist_haircut: float = 0.90        # forced liquidation haircut on delisting
    risk_dd_kill: float = 0.0           # 0 disables drawdown kill-switch in backtests

    # --- research / OOS ---
    oos_trading_days: int = 252         # locked OOS window length
    walkforward_folds: int = 4
    bootstrap_blocks: int = 500
    bootstrap_block_len: int = 21
    max_universe_names: int = 3000      # cross-section cap for research (top by mkt cap)

    # --- runtime ---
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    eod_run_time: str = "17:40"
    research_run_time: str = "21:30"
    job_max_runtime_sec: int = 3600
    strategy_subprocess_timeout: int = 900
    auto_consume_oos: bool = True       # promotion pipeline may spend OOS once per strategy

    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    def db_path(self, name: str) -> Path:
        return self.data_path / name

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> Settings:
    assert_safety()
    defaults: dict[str, Any] = {}
    s = Settings()
    for f in fields(s):
        v = getattr(s, f.name)
        defaults[f.name] = list(v) if isinstance(v, tuple) else v

    merged = dict(defaults)
    for fname in ("settings.json", "settings.local.json"):
        p = ROOT / "config" / fname
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                validate_safety_overrides(raw)
                merged = _deep_merge(merged, raw)
            except Exception as e:  # malformed user file must not brick the platform
                from src.quantcore.contracts.safety import SafetyViolation
                if isinstance(e, SafetyViolation):
                    raise
                print(f"[config] warning: failed to parse {p.name}: {e}")

    # env overrides QF_<NAME>
    for f in fields(Settings):
        env = os.environ.get(f"QF_{f.name.upper()}")
        if env is None or env == "":
            continue
        cur = merged.get(f.name)
        try:
            if isinstance(cur, bool):
                merged[f.name] = env.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                merged[f.name] = int(env)
            elif isinstance(cur, float):
                merged[f.name] = float(env)
            else:
                merged[f.name] = env
        except Exception:
            merged[f.name] = env

    kw = {k: v for k, v in merged.items() if k in {f.name for f in fields(Settings)}}
    # tuple-typed settings
    for tupname in ("benchmark_codes", "etf_pool"):
        if isinstance(kw.get(tupname), list):
            kw[tupname] = tuple(kw[tupname])
    for path_name in ("data_dir", "logs_dir", "archive_dir"):
        configured = Path(kw[path_name]).expanduser()
        kw[path_name] = str((ROOT / configured).resolve() if not configured.is_absolute() else configured.resolve())
    st = Settings(**kw)
    assert_safety(st.dashboard_host)
    boundary = PathBoundary()
    boundary.require_writable(st.data_path)
    boundary.require_writable(st.logs_dir)
    boundary.require_writable(st.archive_dir)
    return st


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
