# -*- coding: utf-8 -*-
"""Performance & factor statistics. Pure functions; no DB access here."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    peak = nav.cummax()
    dd = nav / peak - 1.0
    return float(dd.min())


def drawdown_series(nav: pd.Series) -> pd.Series:
    if nav.empty:
        return pd.Series(dtype=float)
    return nav / nav.cummax() - 1.0


def rolling_sharpe(ret: pd.Series, window: int = 63, rf: float = 0.0) -> pd.Series:
    if ret.empty:
        return pd.Series(dtype=float)
    mu = ret.rolling(window).mean() * TRADING_DAYS
    sd = ret.rolling(window).std() * np.sqrt(TRADING_DAYS)
    return ((mu - rf) / sd).replace([np.inf, -np.inf], np.nan)


def annual_return(nav: pd.Series) -> float:
    if nav.empty or len(nav) < 2:
        return 0.0
    years = len(nav) / TRADING_DAYS
    total = nav.iloc[-1] / nav.iloc[0]
    if total <= 0:
        return -1.0
    return float(total ** (1 / max(years, 1e-9)) - 1)


def perf_stats(nav_df: pd.DataFrame, fills_df: pd.DataFrame | None = None,
               initial_cash: float = 1_000_000.0) -> dict:
    """nav_df: index=trade_date, columns nav/ret/bench_ret/cash/exposure."""
    out = {}
    if nav_df is None or nav_df.empty:
        return {"error": "empty nav"}
    nav = nav_df["nav"].astype(float)
    ret = nav_df["ret"].astype(float)
    years = len(nav) / TRADING_DAYS
    out["start"] = str(nav_df.index[0])
    out["end"] = str(nav_df.index[-1])
    out["days"] = int(len(nav))
    out["total_return"] = float(nav.iloc[-1] / initial_cash - 1)
    out["cagr"] = annual_return(nav)
    out["ann_vol"] = float(ret.std() * np.sqrt(TRADING_DAYS))
    sd = ret.std()
    out["sharpe"] = float(ret.mean() / sd * np.sqrt(TRADING_DAYS)) if sd and sd > 0 else 0.0
    dn = ret[ret < 0]
    dsd = dn.std()
    out["sortino"] = float(ret.mean() / dsd * np.sqrt(TRADING_DAYS)) if dsd and dsd > 0 else 0.0
    out["mdd"] = max_drawdown(nav)
    out["calmar"] = float(out["cagr"] / abs(out["mdd"])) if out["mdd"] < 0 else 0.0
    out["win_rate"] = float((ret > 0).mean())
    out["exposure_avg"] = float(nav_df["exposure"].mean()) if "exposure" in nav_df else None
    bench = nav_df.get("bench_ret")
    if bench is not None and isinstance(bench, pd.Series) and not bench.empty:
        bs = bench.std()
        out["bench_cagr"] = annual_return((1 + bench.fillna(0)).cumprod())
        out["excess_cagr"] = out["cagr"] - out["bench_cagr"]
        exc = ret.fillna(0) - bench.fillna(0)
        if bs and bs > 0:
            out["info_ratio"] = float(exc.mean() / exc.std() * np.sqrt(TRADING_DAYS))
        else:
            out["info_ratio"] = 0.0
        # beta / alpha (annualized, daily regression)
        both = pd.concat([ret, bench], axis=1).dropna()
        if len(both) > 60 and both.iloc[:, 1].std() > 0:
            beta = float(np.cov(both.iloc[:, 0], both.iloc[:, 1])[0, 1] / np.var(both.iloc[:, 1]))
            out["beta"] = beta
            out["alpha_ann"] = float((both.iloc[:, 0] - beta * both.iloc[:, 1]).mean() * TRADING_DAYS)
    if fills_df is not None and not fills_df.empty:
        traded = float((fills_df["shares"] * fills_df["price"]).abs().sum())
        avg_nav = float(nav.mean())
        out["turnover_ann"] = traded / max(avg_nav, 1e-9) / max(years, 1e-9) * 0.5
        out["fees_total"] = float(fills_df["fee"].sum())
        out["cost_drag_ann"] = out["fees_total"] / max(avg_nav, 1e-9) / max(years, 1e-9)
        out["n_trades"] = int(len(fills_df))
    else:
        out["turnover_ann"] = 0.0
        out["fees_total"] = 0.0
        out["cost_drag_ann"] = 0.0
        out["n_trades"] = 0
    return out


# ------------------------------------------------------------------ factors ----
def rank_ic_series(factor_wide: pd.DataFrame, fwd_ret_wide: pd.DataFrame) -> pd.Series:
    """Daily Spearman rank IC between factor and forward returns."""
    common = factor_wide.index.intersection(fwd_ret_wide.index)
    out = {}
    for d in common:
        f = factor_wide.loc[d]
        r = fwd_ret_wide.loc[d]
        ok = f.notna() & r.notna()
        if ok.sum() < 30:
            continue
        out[d] = float(f[ok].corr(r[ok], method="spearman"))
    return pd.Series(out)


def factor_stats(ic: pd.Series, long_short_ann: float | None = None,
                 turnover: float | None = None) -> dict:
    if ic.empty:
        return {"ic_mean": None, "rank_ic_mean": None, "ic_ir": None,
                "ic_tstat": None, "n_days": 0}
    n = len(ic)
    mean = float(ic.mean())
    sd = float(ic.std())
    tstat = mean / (sd / np.sqrt(n)) if sd > 0 and n > 2 else 0.0
    return {"ic_mean": mean, "rank_ic_mean": mean, "ic_ir": float(mean / sd) if sd > 0 else 0.0,
            "ic_tstat": float(tstat), "n_days": int(n),
            "long_short_ann": long_short_ann, "turnover": turnover,
            "ic_positive_ratio": float((ic > 0).mean())}


def quantile_spread(factor_wide: pd.DataFrame, fwd_ret_wide: pd.DataFrame,
                    n_q: int = 5) -> dict:
    """Equal-weight quantile portfolio mean daily returns + long-short spread."""
    common = factor_wide.index.intersection(fwd_ret_wide.index)
    q_rets = np.zeros(n_q)
    ls_rets = []
    counts = 0
    for d in common:
        f = factor_wide.loc[d].dropna()
        r = fwd_ret_wide.loc[d]
        if len(f) < 50:
            continue
        try:
            buckets = pd.qcut(f.rank(method="first"), n_q, labels=False)
        except ValueError:
            continue
        port = np.zeros(n_q)
        for q in range(n_q):
            names = buckets[buckets == q].index
            if len(names):
                port[q] = float(r.reindex(names).mean())
        q_rets += port
        ls_rets.append(port[-1] - port[0])
        counts += 1
    if counts == 0:
        return {"quantile_daily": [0.0] * n_q, "long_short_ann": 0.0, "monotonic": False}
    q_daily = (q_rets / counts).tolist()
    ls = pd.Series(ls_rets)
    ls_ann = float(ls.mean() * TRADING_DAYS)
    mono = all(q_daily[i] <= q_daily[i + 1] + 1e-12 for i in range(n_q - 1)) or \
           all(q_daily[i] >= q_daily[i + 1] - 1e-12 for i in range(n_q - 1))
    return {"quantile_daily": q_daily, "long_short_ann": ls_ann,
            "monotonic": bool(mono), "n_days": counts}


def bootstrap_sharpe_ci(ret: pd.Series, n_blocks: int = 500, block_len: int = 21,
                        seed: int = 7) -> dict:
    """Moving-block bootstrap CI of annualized Sharpe. Robustness >> return."""
    r = ret.dropna().to_numpy()
    if len(r) < block_len * 3:
        return {"lo": None, "hi": None, "mean": None, "n": int(len(r))}
    rng = np.random.default_rng(seed)
    n = len(r)
    n_chunks = int(np.ceil(n / block_len))
    starts_max = n - block_len
    stats = np.empty(n_blocks)
    for b in range(n_blocks):
        starts = rng.integers(0, starts_max, size=n_chunks)
        sample = np.concatenate([r[s:s + block_len] for s in starts])[:n]
        sd = sample.std(ddof=1)
        stats[b] = (sample.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    return {"lo": float(np.percentile(stats, 5)), "hi": float(np.percentile(stats, 95)),
            "mean": float(stats.mean()), "n": int(n)}


def neutralize(factor_s: pd.Series, industries: pd.Series | None = None,
               market_caps: pd.Series | None = None) -> pd.Series:
    """Regress factor series against industry dummies and log(market_cap).
    Returns standardized (z-scored) residuals orthogonal to sector & size.
    """
    valid = factor_s.dropna()
    if len(valid) < 20:
        return factor_s
    idx = valid.index
    parts = []
    if industries is not None:
        ind = industries.reindex(idx).fillna("OTHER")
        dummies = pd.get_dummies(ind, drop_first=True, dtype=float)
        parts.append(dummies)
    if market_caps is not None:
        mv = market_caps.reindex(idx)
        ln_mv = np.log(mv.where(mv > 0)).replace([np.inf, -np.inf], np.nan)
        ln_mv = ln_mv.fillna(ln_mv.median())
        parts.append(ln_mv.rename("ln_mv"))
    if not parts:
        sd = valid.std()
        return (valid - valid.mean()) / sd if sd > 0 else valid
    X = pd.concat(parts, axis=1)
    X["const"] = 1.0
    try:
        beta, _, _, _ = np.linalg.lstsq(X.values, valid.values, rcond=None)
        resid = valid.values - X.values @ beta
        out = pd.Series(resid, index=idx)
        sd = out.std()
        return (out - out.mean()) / sd if sd > 0 else out
    except Exception:
        sd = valid.std()
        return (valid - valid.mean()) / sd if sd > 0 else valid
