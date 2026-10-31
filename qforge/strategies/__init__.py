# -*- coding: utf-8 -*-
"""Strategy Registry + strategy implementations.

Every strategy:
- has an id, category, documented economic rationale, default params
- emits TARGET WEIGHTS as of the close of day T; the engine executes at T+1
- reads only PIT data (ctx.pit) and stored factor features
- never sees future bars (PITReader strict mode raises on violation)

Strategies are deliberately simple and economically motivated. No brute-force
parameter sweeps: sensitivity is *tested* by the evaluator, not optimized away.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.engine import BaseStrategy, StrategyContext
from .. import db
from ..config import get_settings
from ..util import get_logger, now_ts, sha1

log = get_logger("qforge.strat")

REGISTRY: dict[str, type[BaseStrategy]] = {}


def register(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    REGISTRY[cls.strategy_id] = cls
    return cls


def sync_registry() -> None:
    lab = db.connect_lab()
    try:
        rows = []
        for sid, cls in REGISTRY.items():
            rows.append({
                "strategy_id": sid, "name": cls.name, "description": cls.description,
                "category": cls.category, "params_json": db.json_dumps(cls.default_params),
                "version": sha1([sid, cls.default_params])[:8],
                "created_at": now_ts(), "status": "RESEARCH",
            })
        db.upsert(lab, "strategy_registry", rows,
                  ["strategy_id", "name", "description", "category", "params_json",
                   "version", "created_at", "status"])
    finally:
        lab.close()


# ------------------------------------------------------------------ helpers ----
def _month_key(d: str) -> str:
    return d[:6]


def _week_key(d: str) -> str:
    import datetime as dt
    iso = dt.datetime.strptime(d, "%Y%m%d").isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def rebal_on(freq: str, ctx: StrategyContext, state: dict) -> bool:
    """First open session of each month / ISO week; 'D' = every day."""
    if freq == "D":
        return True
    key = _month_key(ctx.date) if freq == "M" else _week_key(ctx.date)
    if state.get("_last_rebal") != key:
        state["_last_rebal"] = key
        return True
    return False


def feature_z(pit, factor_id: str) -> pd.Series | None:
    """z-scored latest PIT cross-section, or None if the factor has no data."""
    df = pit.features_latest(factor_id)
    if df.empty:
        return None
    s = df.set_index("ts_code")["value"].astype(float)
    sd = s.std()
    if not sd or np.isnan(sd) or sd == 0:
        return None
    return (s - s.mean()) / sd


def feature_raw(pit, factor_id: str) -> pd.Series | None:
    df = pit.features_latest(factor_id)
    if df.empty:
        return None
    return df.set_index("ts_code")["value"].astype(float)


def avg_amount(ctx: StrategyContext, code: str, n: int = 20) -> float | None:
    """Trailing n-day average amount (PIT: up to decision date)."""
    bs = ctx.bars
    if bs.amount.empty or code not in bs.amount.columns:
        return None
    if n == 20:
        if not hasattr(bs, "_amount_rolling20"):
            bs._amount_rolling20 = bs.amount.rolling(20, min_periods=max(5, 20 // 2)).mean()
        if ctx.date in bs._amount_rolling20.index:
            v = bs._amount_rolling20.loc[ctx.date, code]
            return float(v) if pd.notna(v) else None
    idx = bs.amount.index
    try:
        pos = idx.get_loc(ctx.date)
    except KeyError:
        pos = len(idx) - 1
    lo = max(0, pos - n + 1)
    w = bs.amount.iloc[lo:pos + 1][code].dropna()
    return float(w.mean()) if len(w) >= max(5, n // 2) else None


def liquid_universe(ctx: StrategyContext, min_amount: float = 3e7) -> list[str]:
    bs = ctx.bars
    if bs.amount.empty:
        return []
    uni = ctx.bars.universe(ctx.date)
    if not uni:
        return []
    if not hasattr(bs, "_amount_rolling20"):
        bs._amount_rolling20 = bs.amount.rolling(20, min_periods=max(5, 20 // 2)).mean()
    if ctx.date not in bs._amount_rolling20.index:
        return []
    s = bs._amount_rolling20.loc[ctx.date]
    valid = s[(s >= min_amount) & s.index.isin(uni)]
    return valid.index.tolist()


def to_cash_weights(ctx: StrategyContext, targets: dict[str, float]) -> dict[str, float]:
    """Include explicit 0-weights for held names so the engine sells them."""
    out = dict(targets)
    for code in list(ctx.positions):
        if code not in out:
            out[code] = 0.0
    return out


def top_equal(composite: pd.Series, n: int) -> dict[str, float]:
    comp = composite.dropna()
    comp = comp[comp > -np.inf]
    if len(comp) < max(5, n // 3):
        return {}
    picks = comp.nlargest(n).index.tolist()
    return {c: 1.0 / len(picks) for c in picks}


# ------------------------------------------------------------------ strategies ----
@register
class DefensiveQuality(BaseStrategy):
    """低波+低贝塔+质量(或有EP)复合。经济逻辑：低风险异象与盈利质量溢价，
    防御型组合，追求风险调整后收益而非绝对收益。"""
    strategy_id = "S1_DEFENSIVE_QUALITY"
    name = "防御质量复合(月度)"
    category = "multi-factor"
    description = ("z(低波动60)+z(低Beta252)+z(质量综合|EP)：低风险异象叠加盈利质量，"
                   "月度调仓、等权Top30、只做production eligible证券")
    default_params = {"top_n": 30, "freq": "M", "min_amount": 3e7}
    required_factors = ("LOWVOL_60", "LOWBETA_252", "QUALITY_COMP")

    def target_weights(self, ctx, params):
        if not rebal_on(params.get("freq", "M"), ctx, ctx.state):
            return {}
        z_vol = feature_z(ctx.pit, "LOWVOL_60")
        z_beta = feature_z(ctx.pit, "LOWBETA_252")
        z_q = feature_z(ctx.pit, "QUALITY_COMP")
        z_ep = feature_z(ctx.pit, "EP")
        comps = [z for z in (z_vol, z_beta, z_q if z_q is not None else z_ep) if z is not None]
        if len(comps) < 2:
            log.info("S1: insufficient factor coverage at %s", ctx.date)
            return {}
        comp = sum(comps).dropna()
        uni = set(liquid_universe(ctx, params["min_amount"]))
        comp = comp[comp.index.isin(uni)]
        return top_equal(comp, params["top_n"])


@register
class Momentum121(BaseStrategy):
    """经典 12-1 月动量。A股动量历史偏弱，用于诚实检验而非预期必胜。"""
    strategy_id = "S2_MOMENTUM_12_1"
    name = "动量12-1(月度)"
    category = "momentum"
    description = "z(12-1月动量) Top50 等权，月度调仓，流动性过滤；A股动量弱反转强的诚实检验"
    default_params = {"top_n": 50, "freq": "M", "min_amount": 3e7}
    required_factors = ("MOM12_1",)

    def target_weights(self, ctx, params):
        if not rebal_on(params.get("freq", "M"), ctx, ctx.state):
            return {}
        z = feature_z(ctx.pit, "MOM12_1")
        if z is None:
            return {}
        uni = set(liquid_universe(ctx, params["min_amount"]))
        return top_equal(z[z.index.isin(uni)], params["top_n"])


@register
class Reversal5D(BaseStrategy):
    """5日反转。高换手、高成本，预期可能被费用吞噬——用于量化成本敏感性。"""
    strategy_id = "S3_REVERSAL_5D"
    name = "短期反转5日(周度)"
    category = "reversal"
    description = "z(5日反转) Top30 等权，周度调仓；检验超跌反弹在T+1+费用约束下是否存活"
    default_params = {"top_n": 30, "freq": "W", "min_amount": 5e7}
    required_factors = ("REV_5",)

    def target_weights(self, ctx, params):
        if not rebal_on(params.get("freq", "W"), ctx, ctx.state):
            return {}
        z = feature_z(ctx.pit, "REV_5")
        if z is None:
            return {}
        uni = set(liquid_universe(ctx, params["min_amount"]))
        return top_equal(z[z.index.isin(uni)], params["top_n"])


@register
class ETFRotation(BaseStrategy):
    """ETF 轮动（双动量+趋势过滤）。逻辑：资产间动量 + 站上200日均线才持有，
    否则现金；低换手、容量大、费用低。"""
    strategy_id = "S4_ETF_ROTATION"
    name = "ETF双动量轮动(周度)"
    category = "etf-rotation"
    description = ("ETF池内 风险调整动量(ret60/vol20) Top2 等权；个体ETF跌破MA200则换成现金；"
                   "周度调仓，T+1执行")
    default_params = {"top_n": 2, "freq": "W", "mom_window": 60, "vol_window": 20,
                      "ma_window": 200, "weight_cap": 0.5}
    required_factors = ()
    target_pool = ("510300.SH", "510500.SH", "159915.SZ", "518880.SH", "511010.SH")

    def target_weights(self, ctx, params):
        if not rebal_on(params.get("freq", "W"), ctx, ctx.state):
            return {}
        s = get_settings()
        pool = [c for c in s.etf_pool]
        etf = ctx.pit.etf(codes=pool, start=_shift_days(ctx.date, 420), end=ctx.date)
        if etf.empty:
            return to_cash_weights(ctx, {})
        scores = {}
        for code, g in etf.groupby("ts_code"):
            g = g.sort_values("trade_date")
            if len(g) < params["ma_window"] // 2:
                continue
            close = g.set_index("trade_date")["close"].astype(float)
            ret = close.pct_change().dropna()
            mom = close.iloc[-1] / close.iloc[-params["mom_window"]] - 1 if len(close) > params["mom_window"] else np.nan
            vol = ret.iloc[-params["vol_window"]:].std() if len(ret) > params["vol_window"] else np.nan
            ma = close.iloc[-params["ma_window"]:].mean() if len(close) >= params["ma_window"] else np.nan
            if np.isnan(mom) or np.isnan(vol) or vol <= 0:
                continue
            above = bool(np.isnan(ma) or close.iloc[-1] > ma)
            scores[code] = {"score": mom / vol, "above": above}
        ok = {c: v["score"] for c, v in scores.items() if v["above"]}
        picks = sorted(ok.items(), key=lambda kv: -kv[1])[: params["top_n"]]
        if not picks:
            return to_cash_weights(ctx, {})
        w = min(1.0 / len(picks), params["weight_cap"])
        targets = {c: w for c, _ in picks}
        return to_cash_weights(ctx, targets)


@register
class IndexTrendTiming(BaseStrategy):
    """沪深300趋势择时。逻辑：趋势溢价 + 波动状态过滤；用510300 ETF执行。"""
    strategy_id = "S5_INDEX_TREND_TIMING"
    name = "指数趋势择时(日度)"
    category = "trend"
    description = ("沪深300收盘>MA200且MA20>MA60时满仓510300ETF，否则现金；日度信号、"
                   "引擎涨跌停/停牌约束下执行")
    default_params = {"ma_long": 200, "ma_mid": 60, "ma_short": 20, "etf_code": "510300.SH"}
    required_factors = ()
    target_pool = ("510300.SH",)

    def target_weights(self, ctx, params):
        bench = ctx.pit.bench(codes=["000300.SH"], start=_shift_days(ctx.date, 420), end=ctx.date)
        if bench.empty:
            return {}
        close = bench.sort_values("trade_date").set_index("trade_date")["close"].astype(float)
        if len(close) < params["ma_long"]:
            return {}
        ma_long = close.iloc[-params["ma_long"]:].mean()
        ma_mid = close.iloc[-params["ma_mid"]:].mean()
        ma_short = close.iloc[-params["ma_short"]:].mean()
        w = 1.0 if (close.iloc[-1] > ma_long and ma_short > ma_mid) else 0.0
        return {params["etf_code"]: w}


@register
class EventAlpha(BaseStrategy):
    """公告事件Alpha。逻辑：业绩预告预增/扭亏等正面事件后的短期漂移；
    持有10个交易日。LLM仅辅助结构化，事件→分数→仓位由规则给出。"""
    strategy_id = "S6_EVENT_ALPHA"
    name = "公告事件Alpha"
    category = "event"
    description = "PIT公告事件(预增/扭亏/回购等)评分≥阈值买入，持有H日后卖出；事件库为空则空仓"
    default_params = {"score_min": 2.0, "hold_days": 10, "weight": 0.08, "max_events": 6}
    required_factors = ()

    def target_weights(self, ctx, params):
        state = ctx.state.setdefault("_events", {})
        # 1) expire old positions
        for code in list(state):
            if ctx.date > state[code]["exit_date"]:
                del state[code]
        # 2) fetch fresh events from event store (PIT)
        lab = db.connect_lab()
        try:
            rows = db.q(lab, "SELECT * FROM event_store WHERE ts_code IS NOT NULL "
                             "AND event_ts <= ? AND score >= ? ORDER BY event_ts DESC LIMIT 200",
                        (ctx.pit.decision_instant, params["score_min"]))
        finally:
            lab.close()
        uni = set(ctx.bars.universe(ctx.date))
        for r in rows:
            if r["ts_code"] in state or r["ts_code"] not in uni:
                continue
            if len(state) >= params["max_events"]:
                break
            if r["ts_code"] in ctx.positions:
                continue
            exit_d = _shift_sessions(ctx.date, params["hold_days"])
            if exit_d is None:
                continue
            state[r["ts_code"]] = {"entry_date": ctx.date, "exit_date": exit_d,
                                   "score": r["score"], "kind": r["kind"]}
        targets = {code: params["weight"] for code in state}
        return to_cash_weights(ctx, targets)


@register
class ValueMomentum(BaseStrategy):
    """价值+动量混合。逻辑：便宜且仍有趋势的股票；价值提供安全边际，动量提供时机。"""
    strategy_id = "S7_VALUE_MOM"
    name = "价值+动量(月度)"
    category = "multi-factor"
    description = "z(EP)+z(BP)+0.5*z(12-1动量) Top30 等权，月度调仓"
    default_params = {"top_n": 30, "freq": "M", "min_amount": 3e7}
    required_factors = ("EP", "BP", "MOM12_1")

    def target_weights(self, ctx, params):
        if not rebal_on(params.get("freq", "M"), ctx, ctx.state):
            return {}
        z_ep = feature_z(ctx.pit, "EP")
        z_bp = feature_z(ctx.pit, "BP")
        z_mom = feature_z(ctx.pit, "MOM12_1")
        comps = [z for z in (z_ep, z_bp, z_mom * 0.5 if z_mom is not None else None) if z is not None]
        if not comps:
            return {}
        comp = sum(comps).dropna()
        uni = set(liquid_universe(ctx, params["min_amount"]))
        return top_equal(comp[comp.index.isin(uni)], params["top_n"])


# ------------------------------------------------------------------ utils ----
def _shift_days(d: str, days: int) -> str:
    import datetime as dt
    dd = dt.datetime.strptime(d, "%Y%m%d") - dt.timedelta(days=days)
    return dd.strftime("%Y%m%d")


def _shift_sessions(d: str, n: int) -> str | None:
    from ..data import calendar as cal
    return cal.shift(d, n)
