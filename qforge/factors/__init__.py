# -*- coding: utf-8 -*-
"""Factor Registry + factor library.

Design rules:
- every factor has an id, economic rationale, params and a direction already
  folded in (higher value = more attractive for the long leg).
- computation is vectorized over the window, but each (ts_code, trade_date)
  value only uses information from <= trade_date (backward-looking windows).
- storage stamps available_at: bar-derived -> trade_date 17:00 CST;
  report-derived -> report announcement + lag (PIT-conservative).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import db
from ..config import get_settings
from ..data.pit import PITReader
from ..util import available_at_next_day, get_logger, now_ts, sha1, today_str

log = get_logger("qforge.factors")


# ------------------------------------------------------------------ registry ----
class FactorDef:
    def __init__(self, factor_id: str, name: str, category: str, description: str,
                 params: dict | None = None, needs: tuple = ("bars",)):
        self.factor_id = factor_id
        self.name = name
        self.category = category
        self.description = description
        self.params = params or {}
        self.needs = needs


REGISTRY: dict[str, FactorDef] = {}
COMPUTE: dict[str, callable] = {}


def register(fd: FactorDef, fn: callable | None = None) -> None:
    REGISTRY[fd.factor_id] = fd
    if fn is not None:
        COMPUTE[fd.factor_id] = fn


def sync_registry() -> None:
    lab = db.connect_lab()
    try:
        rows = [{"factor_id": f.factor_id, "name": f.name, "category": f.category,
                 "description": f.description, "params_json": db.json_dumps(f.params),
                 "version": sha1([f.factor_id, f.params])[:8], "created_at": now_ts(),
                 "status": "ACTIVE"} for f in REGISTRY.values()]
        db.upsert(lab, "factor_registry", rows,
                  ["factor_id", "name", "category", "description", "params_json",
                   "version", "created_at", "status"])
    finally:
        lab.close()


# ------------------------------------------------------------------ data prep ----
class FactorData:
    """Wide matrices shared by factor computations (loaded once per run)."""

    def __init__(self, pit: PITReader, codes: list[str], start: str, end: str,
                 warmup_days: int = 320):
        self.codes = sorted(codes)
        from ..data import calendar as cal
        window = cal.open_days_between(start, end)
        idx = max(0, window.index(start) if start in window else 0)
        warm_start = window[max(0, idx - warmup_days)] if window else start
        self.dates = window[idx:] if window else []
        bars = pit.bars(codes=self.codes, start=warm_start, end=end, include_suspended=True)
        self.close = self._wide(bars, "close")
        self.adj = self._wide(bars, "adj_factor")
        self.amount = self._wide(bars, "amount")
        self.adj = self.adj.ffill()
        self.adj_close = self.close * self.adj
        self.ret = self.adj_close.pct_change(fill_method=None)
        s = get_settings()
        bench = pit.bench(codes=[s.benchmark_codes[1]], start=warm_start, end=end)  # 000300.SH
        self.bench_ret = None
        if not bench.empty:
            b = bench.set_index("trade_date")["close"].astype(float)
            self.bench_ret = b.pct_change(fill_method=None).reindex(self.close.index)
        basic = pit.daily_basic(codes=self.codes, start=warm_start, end=end)
        if basic.empty:
            empty = pd.DataFrame(np.nan, index=self.close.index, columns=self.close.columns)
            self.turnover = empty.copy(); self.pe = empty.copy(); self.pb = empty.copy()
            self.dv = empty.copy(); self.mv = empty.copy()
        else:
            self.turnover = self._wide(basic, "turnover_rate_f")
            if self.turnover.isna().all().all():
                self.turnover = self._wide(basic, "turnover_rate")
            self.pe = self._wide(basic, "pe_ttm")
            self.pb = self._wide(basic, "pb")
            self.dv = self._wide(basic, "dv_ttm")
            self.mv = self._wide(basic, "total_mv")
        self.fina = pit.fundamentals(codes=self.codes)

    def _wide(self, df: pd.DataFrame, col: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(np.nan, index=self.dates, columns=self.codes)
        w = df.pivot_table(index="trade_date", columns="ts_code", values=col, aggfunc="last")
        return w.reindex(index=self.dates, columns=self.codes).astype(float)

    def z(self, df: pd.DataFrame) -> pd.DataFrame:
        mu = df.mean(axis=1)
        sd = df.std(axis=1, ddof=0).replace(0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0)

    def melt(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.stack(future_stack=True).rename("value").reset_index()
        out.columns = ["trade_date", "ts_code", "value"]
        return out.dropna(subset=["value"])

    def latest_snapshot(self, fina: pd.DataFrame, d: str, cols: list[str]) -> pd.DataFrame:
        """For each code the most recent report announced by date d."""
        sub = fina[fina["ann"] <= d].groupby("ts_code").tail(1).set_index("ts_code")
        return sub


# ------------------------------------------------------------------ computes ----
def _c_mom12_1(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(d.adj_close.shift(21) / d.adj_close.shift(252) - 1.0)


def _c_lowvol(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(-d.ret.rolling(60, min_periods=40).std())


def _c_downvol(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    dn = d.ret.where(d.ret < 0, 0.0)
    return d.melt(-dn.rolling(60, min_periods=40).std())


def _c_rev5(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(-d.adj_close.pct_change(5, fill_method=None))


def _c_trend(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    ma = d.close.rolling(120, min_periods=80).mean()
    tr = d.close / ma - 1.0
    vol = d.ret.rolling(60, min_periods=40).std()
    return d.melt(tr / vol.replace(0, np.nan))


def _c_amihud(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    am = (d.ret.abs() / d.amount.replace(0, np.nan)).rolling(20, min_periods=12).mean()
    return d.melt(-am * 1e9)


def _c_lturn(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(-d.turnover.rolling(21, min_periods=12).mean())


def _market_beta(d: FactorData) -> pd.DataFrame:
    if d.bench_ret is None:
        return pd.DataFrame(np.nan, index=d.close.index, columns=d.close.columns)
    cov = d.ret.rolling(252, min_periods=120).cov(d.bench_ret)
    var = d.bench_ret.rolling(252, min_periods=120).var().where(lambda x: x > 0)
    return cov.div(var, axis=0)


def _c_lowbeta(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(-_market_beta(d))


def _c_ivol(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    if d.bench_ret is None:
        return d.melt(pd.DataFrame(np.nan, index=d.close.index, columns=d.close.columns))
    beta = _market_beta(d)
    mkt = d.bench_ret.to_numpy()[:, None] * beta.to_numpy()
    resid = pd.DataFrame(d.ret.to_numpy() - mkt, index=d.ret.index, columns=d.ret.columns)
    return d.melt(-resid.rolling(60, min_periods=40).std())


def _c_ep(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(1.0 / d.pe.where(d.pe > 0))


def _c_bp(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(1.0 / d.pb.where(d.pb > 0))


def _c_divy(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(d.dv)


def _c_size(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    return d.melt(-np.log(d.mv.where(d.mv > 0)))


def _pit_report_frame(d: FactorData, cols: list[str]) -> pd.DataFrame:
    """Long frame: one row per (code, ann) with report columns available then."""
    f = d.fina[["ts_code", "f_ann_date", "ann_date", "end_date"] + cols].dropna(subset=["end_date"]).copy()
    f["ann"] = f["f_ann_date"].fillna(f["ann_date"])
    return f.dropna(subset=["ann"]).sort_values(["ts_code", "ann"]).drop_duplicates(subset=["ts_code", "ann"], keep="last")


def _c_roe(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    if d.fina.empty:
        # PB / PE = (P/B) / (P/E) = E/B = ROE (valuation-implied ROE proxy)
        proxy = (d.pb / d.pe.where(d.pe > 0)).replace([np.inf, -np.inf], np.nan)
        df = d.melt(proxy)
        if not df.empty:
            df["available_at"] = df["trade_date"] + " 17:00:00"
        return df
    f = _pit_report_frame(d, ["roe"])
    rows = []
    for day in d.dates:
        sub = f[f["ann"] <= day].groupby("ts_code").tail(1)
        if sub.empty:
            continue
        for ts, roe, ann in zip(sub["ts_code"], sub["roe"], sub["ann"]):
            if pd.notna(roe):
                rows.append((day, ts, float(roe), available_at_next_day(ann)))
    return pd.DataFrame(rows, columns=["trade_date", "ts_code", "value", "available_at"])


def _c_quality(pit, codes, start, end):
    d = FactorData(pit, codes, start, end)
    if d.fina.empty:
        base = d.z(-d.ret.rolling(60, min_periods=40).std()) * 0.5 \
             + d.z(-d.turnover.rolling(21, min_periods=12).mean()) * 0.5
        return d.melt(base)
    f = _pit_report_frame(d, ["roe", "debt_to_assets", "ocf_to_or"])
    rows = []
    for day in d.dates:
        sub = f[f["ann"] <= day].groupby("ts_code").tail(1).set_index("ts_code")
        if sub.empty:
            continue
        wide = {c: pd.DataFrame(np.nan, index=[day], columns=d.close.columns) for c in ("roe", "lev", "ocf")}
        for ts, r in sub.iterrows():
            for key, col in (("roe", "roe"), ("lev", "debt_to_assets"), ("ocf", "ocf_to_or")):
                if pd.notna(r[col]):
                    wide[key].loc[day, ts] = float(r[col])
        comp = d.z(wide["roe"]).fillna(0) - d.z(wide["lev"]).fillna(0) + d.z(wide["ocf"]).fillna(0)
        v = comp.loc[day]
        for ts, ann in zip(sub.index, sub["ann"]):
            if pd.notna(v.get(ts)) and v.get(ts) != 0.0:
                rows.append((day, ts, float(v[ts]), available_at_next_day(ann)))
    return pd.DataFrame(rows, columns=["trade_date", "ts_code", "value", "available_at"])


_DEFS = [
    ("MOM12_1", "动量12-1月", "momentum", "过去12个月收益剔除最近1月(21日)：捕捉中期动量、剔除短期反转分量", _c_mom12_1),
    ("LOWVOL_60", "低波动60日", "defensive", "60日日收益波动率取负：低风险异象", _c_lowvol),
    ("DOWNVOL_60", "下行波动60日", "defensive", "下行半程波动取负：只惩罚下跌侧波动", _c_downvol),
    ("REV_5", "短期反转5日", "reversal", "5日收益取负：超跌反弹短期反转效应", _c_rev5),
    ("TREND_QUAL_120", "趋势质量120日", "trend", "价格/120日均线偏离÷60日波动：风险调整后趋势强度", _c_trend),
    ("AMIHUD_20", "Amihud非流动性", "liquidity", "单位成交额价格冲击取负：非流动性溢价", _c_amihud),
    ("LTURN_21", "低换手21日", "liquidity", "21日平均换手率取负：低关注异象", _c_lturn),
    ("LOWBETA_252", "低Beta", "defensive", "对沪深300的252日Beta取负：低贝塔异象", _c_lowbeta),
    ("IVOL_60", "特质波动60日", "defensive", "市场模型残差波动取负：特质波动异象", _c_ivol),
    ("EP", "盈利收益率EP", "value", "1/PE_TTM：估值锚", _c_ep),
    ("BP", "账面市值比BP", "value", "1/PB：深度价值", _c_bp),
    ("DIVY", "股息率TTM", "value", "股息率：红利成分", _c_divy),
    ("SIZE_LNMC", "小市值", "size", "log总市值取负：规模溢价(拥挤，谨慎)", _c_size),
    ("ROE_PIT", "ROE(PIT)", "quality", "最近一期已披露ROE：盈利能力", _c_roe),
    ("QUALITY_COMP", "质量综合", "quality", "z(ROE)-z(负债率)+z(现金流/营收)；缺基本面退化为防御代理", _c_quality),
]
for fid, name, cat, desc, fn in _DEFS:
    register(FactorDef(fid, name, cat, desc), fn)


# ------------------------------------------------------------------ storage ----
def compute_and_store(factor_ids: list[str] | None = None, start: str | None = None,
                      end: str | None = None) -> dict:
    s = get_settings()
    end = end or today_str()
    start = start or s.history_start
    ids = factor_ids or list(REGISTRY.keys())
    sync_registry()
    mk = db.connect_market()
    try:
        codes = [r["ts_code"] for r in db.q(
            mk, "SELECT DISTINCT ts_code FROM univ_daily WHERE eligible_research=1")]
    finally:
        mk.close()
    if not codes:
        raise RuntimeError("universe empty; build curated data first")
    results = {}
    pit = PITReader(end, strict=False)
    for fid in ids:
        fn = COMPUTE.get(fid)
        if fn is None:
            results[fid] = {"skipped": "no compute fn"}
            continue
        try:
            df = fn(pit, codes, start, end)
        except Exception as e:
            log.exception("factor %s failed", fid)
            results[fid] = {"error": str(e)[:200]}
            continue
        if df.empty:
            results[fid] = {"rows": 0}
            continue
        if "available_at" in df.columns:
            av = df["available_at"]
        else:
            av = df["trade_date"].map(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}T17:00:00+08:00")
        lab = db.connect_lab()
        try:
            with db.tx(lab):
                lab.execute("DELETE FROM features WHERE factor_id=?", (fid,))
                data = list(zip([fid] * len(df), df["ts_code"], df["trade_date"],
                                df["value"].astype(float), av))
                for i in range(0, len(data), 5000):
                    lab.executemany(
                        "INSERT OR REPLACE INTO features (factor_id, ts_code, trade_date, value, available_at)"
                        " VALUES (?,?,?,?,?)", data[i:i + 5000])
            results[fid] = {"rows": len(data)}
        finally:
            lab.close()
        log.info("factor %s: %d values", fid, len(data))
    return results
