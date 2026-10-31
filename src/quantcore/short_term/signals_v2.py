"""Short-term signal V2: evidence-based improvements over V1.

Each V2 family adds ONE specific improvement to address a known V1 weakness.
Improvements are based on market microstructure logic, not parameter optimization.
V1 remains unchanged for comparison. All improvements are documented with rationale.

Privacy: This module does not access holdings, costs, or private data.
"""

from __future__ import annotations

from typing import Any


def _compute_features_v2(group: Any) -> dict[str, float] | None:  # noqa: ANN401
    """Extended features for V2 signals.

    Adds volume-based features that V1 lacks:
    - volume_ratio: today's volume / 20-day median volume
    - pullback_volume_ratio: volume on pullback day relative to recent average
    - volume_climax: whether today shows significant volume increase

    Returns None if data is insufficient or invalid (FAIL-CLOSED).
    """
    import pandas as pd  # type: ignore[import-untyped]  # Lazy import to avoid circular dependency

    if not {"trade_date", "high", "low", "close"}.issubset(group.columns):
        return None
    group = group.sort_values("trade_date").tail(61).copy()
    if len(group) < 60:
        return None

    # Convert price columns to numeric
    for column in ("high", "low", "close"):
        if column in group.columns:
            group[column] = pd.to_numeric(group[column], errors="coerce")

    # Check for NULL/NaN in required price columns - FAIL CLOSED
    if group[["high", "low", "close"]].isna().any().any():
        return None

    close = group["close"]
    prev = close.shift(1)
    high = group["high"]
    low = group["low"]

    # True Range
    tr = pd.concat([
        (high - low).abs(),
        (high - prev).abs(),
        (low - prev).abs()
    ], axis=1).max(axis=1)

    # Volume handling - FAIL CLOSED: require valid volume data
    # Do NOT default to zero - that creates false signals
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
        # No valid volume data - FAIL CLOSED
        return None

    # Check for NULL values in volume after conversion
    if volume.isna().any():
        return None

    # Check for negative volume (data error)
    if (volume < 0).any():
        return None

    # Base features (same as V1)
    features = {
        "close": float(close.iloc[-1]),
        "sma20": float(close.tail(20).mean()),
        "sma60": float(close.tail(60).mean()),
        "atr14": float(tr.tail(14).mean()),
        "ret3": float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) >= 4 else 0.0,
        "ret5": float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0.0,
        "prior_high20": float(high.iloc[-21:-1].max()) if len(high) >= 21 else float(high.max()),
        "amount": float(volume.iloc[-1]) if len(volume) > 0 else 0.0,
        "median_amount20": float(volume.tail(20).median()) if len(volume) >= 20 else float(volume.median()) if len(volume) > 0 else 0.0,
    }

    # V2 additions: volume-based features
    median_vol = max(features["median_amount20"], 1.0)
    features["volume_ratio"] = features["amount"] / median_vol

    # Pullback volume: average volume during the pullback period (days 2-5 ago)
    if len(volume) >= 5:
        pullback_vol = volume.iloc[-5:-1].mean()
        features["pullback_volume_ratio"] = float(volume.iloc[-1] / max(pullback_vol, 1.0))
    else:
        features["pullback_volume_ratio"] = 1.0

    # Volume climax: today's volume > 2x the 20-day median
    features["volume_climax"] = 1.0 if features["volume_ratio"] >= 2.0 else 0.0

    # Consolidation measure: price range over last 10 days relative to ATR
    if len(close) >= 10:
        recent_range = float(high.tail(10).max() - low.tail(10).min())
        features["consolidation_ratio"] = recent_range / max(features["atr14"] * 10, 0.01)
    else:
        features["consolidation_ratio"] = 1.0

    return features


def signals_v2(features: dict[str, float]) -> list[tuple[str, float, dict[str, float]]]:
    """V2 signal families with evidence-based improvements.

    Each family adds ONE specific improvement to V1:

    1. SHORT_TREND_PULLBACK_V2:
       - V1: SMA alignment + 5-day return range
       - V2: + Volume confirmation (pullback on declining volume)
       - Rationale: Pullbacks on low volume indicate lack of selling pressure,
         making the trend continuation more probable.

    2. SHORT_BREAKOUT_CONFIRM_V2:
       - V1: Price above 20d high + 1.5x volume
       - V2: + Consolidation filter (breakout from range, not extended move)
       - Rationale: Breakouts from consolidation have higher success rates than
         breakouts from already extended trends (continuation vs exhaustion).

    3. SHORT_REVERSAL_V2:
       - V1: 3-day drop + SMA60 support + volume >= median
       - V2: + Volume climax requirement (accumulation signal)
       - Rationale: Reversals with significant volume increase indicate buying
         interest (accumulation), not just dead-cat bounces.

    Price zones are validated for ordering and reasonableness.
    """
    from src.quantcore.short_term.data_quality import validate_price_zones

    c = features["close"]
    a = max(features["atr14"], 0.01)
    signals = []

    # ── SHORT_TREND_PULLBACK_V2 ─────────────────────────────────────
    # V1 condition + volume confirmation
    # Improvement: require pullback_volume_ratio < 1.0 (volume declining on pullback)
    if (c > features["sma20"] > features["sma60"] and
        -0.03 <= features["ret5"] <= 0.03 and
        -0.01 <= c / features["sma20"] - 1 <= 0.02 and
        features.get("pullback_volume_ratio", 1.0) < 1.0):  # V2: volume declining
        zones = {
            "entry_low": max(features["sma20"], c - 0.5 * a),
            "entry_high": min(c, features["sma20"] + 0.5 * a),
            "target": c + 1.5 * a,
            "stop": c - a,
            "chase": min(c, features["sma20"] + 0.5 * a) + 0.5 * a,
        }
        validation = validate_price_zones(zones)
        if validation.is_valid:
            signals.append(("SHORT_TREND_PULLBACK_V2", c / features["sma60"] - 1, zones))

    # ── SHORT_BREAKOUT_CONFIRM_V2 ───────────────────────────────────
    # V1 condition + consolidation filter
    # Improvement: require consolidation_ratio < 3.0 (price was in a range)
    breakout = c / features["prior_high20"] - 1 if features["prior_high20"] else 99
    if (0 < breakout <= 0.015 and
        features["amount"] >= 1.5 * features["median_amount20"] and
        features.get("consolidation_ratio", 1.0) < 3.0):  # V2: from consolidation
        zones = {
            "entry_low": features["prior_high20"],
            "entry_high": features["prior_high20"] + 0.5 * a,
            "target": c + 1.5 * a,
            "stop": features["prior_high20"] - 0.75 * a,
            "chase": features["prior_high20"] + a,
        }
        validation = validate_price_zones(zones)
        if validation.is_valid:
            signals.append(("SHORT_BREAKOUT_CONFIRM_V2", breakout + features["amount"] / max(features["median_amount20"], 1), zones))

    # ── SHORT_REVERSAL_V2 ───────────────────────────────────────────
    # V1 condition + volume climax
    # Improvement: require volume_ratio >= 1.5 (significant buying interest)
    if (features["ret3"] <= -0.05 and
        c >= 0.95 * features["sma60"] and
        features["amount"] >= features["median_amount20"] and
        features.get("volume_ratio", 0.0) >= 1.5):  # V2: volume climax
        zones = {
            "entry_low": c - 0.5 * a,
            "entry_high": c + 0.2 * a,
            "target": c + a,
            "stop": c - a,
            "chase": c + 0.7 * a,
        }
        validation = validate_price_zones(zones)
        if validation.is_valid:
            signals.append(("SHORT_REVERSAL_V2", -features["ret3"], zones))

    return signals
