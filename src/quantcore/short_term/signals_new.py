"""New short-term signal families V1: substantively different from existing families.

These two new families address gaps in the existing signal coverage:

1. SHORT_GAP_MOMENTUM_V1: Gap-up continuation
   - Existing families cover: trend pullback, breakout, reversal
   - This covers: opening gaps with momentum continuation
   - Hypothesis: Gaps on high volume indicate institutional accumulation
   - Different from BREAKOUT: focuses on the gap event itself, not prior consolidation

2. SHORT_RELATIVE_STRENGTH_V1: Market-relative strength
   - Existing families use absolute price levels
   - This compares stock performance to market index
   - Hypothesis: Stocks holding up during market weakness show relative strength
   - Requires: market index data (510300.SH as proxy)

Both families use only daily OHLCV data (no PIT, no minute data needed).
All parameters are based on market microstructure logic, not optimized.

Privacy: No private data access.
"""

from __future__ import annotations

from typing import Any


def _compute_gap_features(
    group: Any,  # noqa: ANN401
    market_close: list[float] | None = None,
) -> dict[str, float] | None:
    """Extended features for gap and relative strength signals.

    Args:
        group: Stock daily data sorted by trade_date
        market_close: Optional market index close prices (same length as group)

    Returns:
        Feature dict or None if insufficient data
    """
    import pandas as pd  # type: ignore[import-untyped]

    group = group.sort_values("trade_date").tail(61).copy()
    if len(group) < 20:
        return None

    for col in ("high", "low", "close", "open", "vol", "volume", "amount"):
        if col in group.columns:
            group[col] = pd.to_numeric(group[col], errors="coerce")

    required = {"high", "low", "close", "open"}
    if not required.issubset(set(group.columns)):
        return None
    if group[list(required)].isna().any().any():
        return None

    close = group["close"]
    open_ = group["open"]
    high = group["high"]
    low = group["low"]
    prev_close = close.shift(1)

    # Volume - FAIL CLOSED: require valid volume data, do not default to zero
    volume_col = None
    if "vol" in group.columns and not group["vol"].isna().all():
        volume_col = "vol"
        volume = pd.to_numeric(group["vol"], errors="coerce")
    elif "volume" in group.columns and not group["volume"].isna().all():
        volume_col = "volume"
        volume = pd.to_numeric(group["volume"], errors="coerce")
    elif "amount" in group.columns and not group["amount"].isna().all():
        volume_col = "amount"
        volume = pd.to_numeric(group["amount"], errors="coerce")
    else:
        # No valid volume data - cannot compute volume-based signals
        return None

    # Check for NULL after conversion
    if volume.isna().any():
        return None

    # Check for negative volume (data error)
    if (volume < 0).any():
        return None

    # True Range
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    features = {
        "close": float(close.iloc[-1]),
        "open": float(open_.iloc[-1]),
        "prev_close": float(prev_close.iloc[-1]) if not pd.isna(prev_close.iloc[-1]) else 0.0,
        "sma20": float(close.tail(20).mean()),
        "sma60": float(close.tail(60).mean()) if len(close) >= 60 else float(close.mean()),
        "atr14": float(tr.tail(14).mean()),
        "amount": float(volume.iloc[-1]),
        "median_amount20": float(volume.tail(20).median()) if len(volume) >= 20 else float(volume.median()) if len(volume) > 0 else 0.0,
    }

    # Gap features
    if features["prev_close"] > 0:
        features["gap_pct"] = (features["open"] / features["prev_close"] - 1)
        features["gap_up"] = 1.0 if features["gap_pct"] > 0.01 else 0.0  # >1% gap up
        features["gap_down"] = 1.0 if features["gap_pct"] < -0.01 else 0.0
    else:
        features["gap_pct"] = 0.0
        features["gap_up"] = 0.0
        features["gap_down"] = 0.0

    features["volume_ratio"] = features["amount"] / max(features["median_amount20"], 1.0)

    # Relative strength (if market data provided)
    # CRITICAL FIX: Align market data by length, not by date
    # Assumption: market_close is a list of prices in same chronological order as group
    # We need at least 21 observations (current + prior 20) to compute 20-day return
    if market_close and len(market_close) >= 21 and len(close) >= 21:
        # Take the most recent observations that match the group length
        # Both stock and market should have same observation count
        if len(market_close) >= len(group):
            market = market_close[-len(group):]
        else:
            # Not enough market data for this stock's history
            features["relative_strength_20"] = 0.0
            features["market_weak"] = 0.0
            return features

        # Verify we have enough data for 20-day lookback
        if len(market) >= 21 and market[-21] > 0:
            # Stock return: close[-1] / close[-21] - 1
            # Market return: market[-1] / market[-21] - 1
            # Time alignment: both use position -1 (most recent) and -21 (21 days ago)
            stock_ret20 = float(close.iloc[-1] / close.iloc[-21] - 1)
            market_ret20 = market[-1] / market[-21] - 1
            features["relative_strength_20"] = float(stock_ret20 - market_ret20)
        else:
            features["relative_strength_20"] = 0.0

        # Market weakness: market below its 20-day SMA
        # Use most recent 20 observations for SMA, current observation for comparison
        if len(market) >= 20:
            market_sma20 = sum(market[-20:]) / 20  # Average of last 20 observations
            features["market_weak"] = 1.0 if market[-1] < market_sma20 else 0.0
        else:
            features["market_weak"] = 0.0
    else:
        features["relative_strength_20"] = 0.0
        features["market_weak"] = 0.0

    return features


def signals_new_families(
    features: dict[str, float],
) -> list[tuple[str, float, dict[str, float]]]:
    """New signal families with substantive differentiation.

    Returns list of (family_name, score, zones) tuples.
    Price zones are validated for ordering and reasonableness.
    """
    from src.quantcore.short_term.data_quality import validate_price_zones

    c = features["close"]
    a = max(features["atr14"], 0.01)
    signals = []

    # ── SHORT_GAP_MOMENTUM_V1 ───────────────────────────────────────
    # Hypothesis: Gap-up on high volume continues due to institutional flow
    # Conditions:
    #   1. Open gaps up >1% from previous close
    #   2. Volume > 2x 20-day median (institutional interest)
    #   3. Close stays above open (no reversal)
    #   4. Price above SMA20 (trend confirmation)
    # Entry zone: gap area (previous close to today's open)
    # Risk: gap fill (price returns to previous close)
    if (features.get("gap_up", 0) > 0 and
        features.get("volume_ratio", 0) >= 2.0 and
        c > features.get("open", c) and
        c > features.get("sma20", 0)):
        prev = features.get("prev_close", c * 0.99)
        gap_open = features.get("open", c)
        zones = {
            "entry_low": prev,  # bottom of gap
            "entry_high": gap_open,  # top of gap
            "target": c + 1.0 * a,
            "stop": prev - 0.5 * a,  # gap fill + buffer
            "chase": c + 0.5 * a,
        }
        validation = validate_price_zones(zones)
        if validation.is_valid:
            signals.append((
                "SHORT_GAP_MOMENTUM_V1",
                features["gap_pct"] * features["volume_ratio"],  # score: gap * volume
                zones
            ))

    # ── SHORT_RELATIVE_STRENGTH_V1 ──────────────────────────────────
    # Hypothesis: Stocks holding up during market weakness show strength
    # Conditions:
    #   1. Market is weak (below its 20-day SMA)
    #   2. Stock has positive relative strength over 20 days
    #   3. Stock is not falling (close > prev close)
    #   4. Volume is reasonable (not panic selling)
    # Entry zone: current price area (buy the relative strength)
    # Risk: market-wide sell-off overwhelms relative strength
    if (features.get("market_weak", 0) > 0 and
        features.get("relative_strength_20", 0) > 0.02 and  # >2% outperformance
        c > features.get("prev_close", c) and
        features.get("volume_ratio", 1.0) < 3.0):  # not panic volume
        zones = {
            "entry_low": c - 0.3 * a,
            "entry_high": c + 0.2 * a,
            "target": c + 1.2 * a,
            "stop": c - 1.0 * a,
            "chase": c + 0.8 * a,
        }
        validation = validate_price_zones(zones)
        if validation.is_valid:
            signals.append((
                "SHORT_RELATIVE_STRENGTH_V1",
                features["relative_strength_20"],  # score: outperformance magnitude
                zones
            ))

    return signals
