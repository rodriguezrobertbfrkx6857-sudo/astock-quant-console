"""Cross-sectional factor evaluation on the partitioned price-only data plane."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from src.quantcore.contracts.config import V2Config
from src.quantcore.data.hashing import canonical_json_bytes
from src.quantcore.data.market_store import PartitionedMarketStore
from src.quantcore.factors.price import FactorDefinition
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService
from src.quantcore.research.registry import canonical_hash


MIN_CROSS_SECTION = 300
PROFILE = "A_SHARE_PRICE_ONLY"
_FRAME_CACHE: tuple[str, pd.DataFrame] | None = None


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _publication(cfg: V2Config) -> tuple[dict[str, Any], Path]:
    pointer_path = cfg.paths.data / "market_store" / "published" / f"{PROFILE}.json"
    pointer = json.loads(pointer_path.read_text("utf-8"))
    manifest_path = Path(str(pointer["manifest_path"]))
    manifest = json.loads(manifest_path.read_text("utf-8"))
    body = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    actual = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if actual != pointer["manifest_hash"] or actual != manifest["manifest_hash"]:
        raise RuntimeError("market data publication manifest hash mismatch")
    return manifest, manifest_path.parent


def _factor_series(frame: pd.DataFrame, definition: FactorDefinition) -> pd.Series:
    grouped = frame.groupby("ts_code", sort=False, observed=True)
    family = definition.family_id
    lookback = definition.lookback
    if family in {"PRICE_MOMENTUM", "PRICE_REVERSAL", "PRICE_TREND_STRENGTH"}:
        momentum = grouped["close"].pct_change(periods=lookback, fill_method=None)
        if family == "PRICE_MOMENTUM":
            return momentum
        if family == "PRICE_REVERSAL":
            return -momentum
        volatility = grouped["_return"].transform(
            lambda values: values.rolling(lookback, min_periods=lookback).std(ddof=0)
        )
        return momentum / volatility.replace(0, float("nan"))
    if family == "PRICE_LOW_VOL":
        return -grouped["_return"].transform(
            lambda values: values.rolling(lookback, min_periods=lookback).std(ddof=0)
        )
    if family == "PRICE_LIQUIDITY":
        average = grouped["amount"].transform(
            lambda values: values.rolling(lookback, min_periods=lookback).mean()
        )
        return average.where(average > 0).map(
            lambda value: math.log(float(value)) if pd.notna(value) else float("nan")
        )
    if family == "VOLUME_SURPRISE":
        average = grouped["vol"].transform(
            lambda values: values.shift(1).rolling(lookback, min_periods=lookback).mean()
        )
        return frame["vol"] / average.replace(0, float("nan")) - 1.0
    raise ValueError(f"factor dependency is not available in price-only profile: {family}")


def _base_frame(cfg: V2Config, manifest_hash: str) -> pd.DataFrame:
    global _FRAME_CACHE
    if _FRAME_CACHE is not None and _FRAME_CACHE[0] == manifest_hash:
        return _FRAME_CACHE[1]
    store = PartitionedMarketStore(
        cfg.paths.data / "market_store", cfg.paths.state / "market_catalog.sqlite"
    )
    table = store.scan(
        profile=PROFILE,
        dataset="daily",
        columns=("ts_code", "trade_date", "close", "vol", "amount", "available_at"),
    )
    frame = table.to_pandas()
    for column in ("close", "vol", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[frame["close"].gt(0)].sort_values(
        ["ts_code", "trade_date"], kind="stable"
    )
    grouped = frame.groupby("ts_code", sort=False, observed=True)
    frame["_return"] = grouped["close"].pct_change(fill_method=None)
    frame["_forward_1"] = grouped["close"].shift(-1) / frame["close"] - 1.0
    frame["_forward_5"] = grouped["close"].shift(-5) / frame["close"] - 1.0
    frame["_forward_20"] = grouped["close"].shift(-20) / frame["close"] - 1.0
    _FRAME_CACHE = (manifest_hash, frame)
    return frame


def evaluate_large_price_factor(
    cfg: V2Config, definition: FactorDefinition, registry_row: sqlite3.Row
) -> dict[str, Any]:
    if definition.family_id in {"TURNOVER_RATE", "BETA", "IDIOSYNCRATIC_VOLATILITY"}:
        return {
            "status": "BLOCKED_DATA",
            "factor_version_id": definition.factor_version_id,
            "reason": (
                "reliable daily_basic turnover history is required"
                if definition.family_id == "TURNOVER_RATE"
                else "a validated HS300 historical benchmark profile is required"
            ),
        }
    manifest, _ = _publication(cfg)
    # Authorize the requested window before loading any observations or
    # calculating a factor.  The later receipt remains bound to the exact
    # realised snapshot used by the persisted evaluation.
    preaccess_snapshot_hash = canonical_hash(
        {
            "factor_version_id": definition.factor_version_id,
            "market_manifest_hash": manifest["manifest_hash"],
            "requested_start": definition.evaluation_start,
            "requested_end": definition.evaluation_end,
            "purpose": "PREACCESS",
        }
    )
    preaccess_receipt = OOSAccessService(
        cfg.paths.state / "control.sqlite"
    ).authorize_known(
        protocol_hash=hashlib.sha256(
            f"factor:{definition.factor_version_id}".encode()
        ).hexdigest(),
        snapshot_hash=preaccess_snapshot_hash,
        start=datetime.strptime(definition.evaluation_start, "%Y-%m-%d").date(),
        end=datetime.strptime(definition.evaluation_end, "%Y-%m-%d").date(),
        purpose=AccessPurpose.RESEARCH,
        asset_scope="A_SHARE_PRICE_ONLY",
        family_scope=definition.family_id,
    )
    if preaccess_receipt.decision is not AccessDecision.ALLOW_KNOWN:
        return {
            "status": "BLOCKED_DATA",
            "factor_version_id": definition.factor_version_id,
            "oos_decision": preaccess_receipt.decision.value,
            "reason": preaccess_receipt.reason,
            "stage": "PREACCESS",
        }
    base = _base_frame(cfg, str(manifest["manifest_hash"]))
    frame = base.copy(deep=False)
    frame["factor_value"] = _factor_series(frame, definition)
    selected = frame.loc[
        frame["trade_date"].between(
            definition.evaluation_start.replace("-", ""),
            definition.evaluation_end.replace("-", ""),
        ),
        [
            "ts_code",
            "trade_date",
            "available_at",
            "factor_value",
            "_forward_1",
            "_forward_5",
            "_forward_20",
        ],
    ].dropna(subset=["factor_value", "_forward_1"])
    if selected.empty:
        raise RuntimeError("large-universe factor produced no observations")
    lower = selected.groupby("trade_date", observed=True)["factor_value"].transform(
        lambda values: values.quantile(0.01)
    )
    upper = selected.groupby("trade_date", observed=True)["factor_value"].transform(
        lambda values: values.quantile(0.99)
    )
    selected["factor_value"] = selected["factor_value"].clip(lower=lower, upper=upper)
    counts = selected.groupby("trade_date", observed=True).size()
    eligible_dates = counts[counts >= MIN_CROSS_SECTION].index
    selected = selected[selected["trade_date"].isin(eligible_dates)]
    if selected.empty:
        raise RuntimeError("cross section never reaches the minimum required size")

    def daily_corr(column: str, *, ranked: bool) -> pd.Series:
        def corr(group: pd.DataFrame) -> float:
            left = group["factor_value"]
            right = group[column]
            if ranked:
                left, right = left.rank(method="average"), right.rank(method="average")
            return float(left.corr(right))

        return selected.dropna(subset=[column]).groupby(
            "trade_date", observed=True, sort=True
        ).apply(corr, include_groups=False)

    ic = daily_corr("_forward_1", ranked=False).dropna()
    rank_ic = daily_corr("_forward_1", ranked=True).dropna()
    decay = {
        horizon: _safe_number(daily_corr(column, ranked=True).dropna().mean())
        for horizon, column in ((1, "_forward_1"), (5, "_forward_5"), (20, "_forward_20"))
    }
    selected["rank_pct"] = selected.groupby("trade_date", observed=True)[
        "factor_value"
    ].rank(pct=True, method="average")
    top = selected[selected["rank_pct"] >= 0.8]
    bottom = selected[selected["rank_pct"] <= 0.2]
    top_daily = top.groupby("trade_date", observed=True)["_forward_1"].mean()
    bottom_daily = bottom.groupby("trade_date", observed=True)["_forward_1"].mean()
    top_sets = [set(group["ts_code"]) for _, group in top.groupby("trade_date", sort=True)]
    turnover_values = [
        1.0 - len(left & right) / max(1, len(left | right))
        for left, right in zip(top_sets, top_sets[1:])
    ]
    ordered_dates = sorted(selected["trade_date"].unique())
    split_1 = max(1, len(ordered_dates) // 3)
    split_2 = max(split_1 + 1, 2 * len(ordered_dates) // 3)
    thirds = [
        ordered_dates[:split_1],
        ordered_dates[split_1:split_2],
        ordered_dates[split_2:],
    ]
    subperiod_rank_ic = [
        _safe_number(
            selected[selected["trade_date"].isin(dates)]
            .groupby("trade_date", observed=True)
            .apply(
                lambda group: group["factor_value"].rank().corr(
                    group["_forward_1"].rank()
                ),
                include_groups=False,
            )
            .mean()
        )
        for dates in thirds
    ]
    evaluation_start = str(min(ordered_dates))
    evaluation_end = str(max(ordered_dates))
    snapshot_hash = canonical_hash(
        {
            "factor_version_id": definition.factor_version_id,
            "market_manifest_hash": manifest["manifest_hash"],
            "start": evaluation_start,
            "end": evaluation_end,
        }
    )
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=hashlib.sha256(
            f"factor:{definition.factor_version_id}".encode()
        ).hexdigest(),
        snapshot_hash=snapshot_hash,
        start=datetime.strptime(evaluation_start, "%Y%m%d").date(),
        end=datetime.strptime(evaluation_end, "%Y%m%d").date(),
        purpose=AccessPurpose.RESEARCH,
        asset_scope="A_SHARE_PRICE_ONLY",
        family_scope=definition.family_id,
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        return {
            "status": "BLOCKED_DATA",
            "factor_version_id": definition.factor_version_id,
            "oos_decision": receipt.decision.value,
            "reason": receipt.reason,
        }
    evaluation_id = (
        f"factor-eval-{definition.factor_version_id.replace('.', '-')}-{snapshot_hash[:20]}"
    )
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT * FROM factor_evaluations WHERE evaluation_id=?", (evaluation_id,)
        ).fetchone()
    finally:
        conn.close()
    if existing is not None:
        return {
            "status": str(existing["status"]),
            "evaluation_id": evaluation_id,
            "factor_version_id": definition.factor_version_id,
            "metrics": json.loads(str(existing["metrics_json"])),
            "evidence_ref": str(existing["evidence_ref"]),
            "idempotent": True,
        }
    rank_std = float(rank_ic.std(ddof=1)) if len(rank_ic) > 1 else 0.0
    denominator = max(1, len(frame[frame["trade_date"].isin(ordered_dates)]))
    metrics = {
        "factor_id": definition.factor_id,
        "factor_version_id": definition.factor_version_id,
        "input_manifest_hash": manifest["manifest_hash"],
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "sample_dates": len(ordered_dates),
        "factor_observations": len(selected),
        "cross_section_min": int(counts.loc[eligible_dates].min()),
        "cross_section_median": _safe_number(counts.loc[eligible_dates].median()),
        "cross_section_max": int(counts.loc[eligible_dates].max()),
        "min_cross_section_required": MIN_CROSS_SECTION,
        "coverage": len(selected) / denominator,
        "missing_ratio": 1.0 - len(selected) / denominator,
        "ic": _safe_number(ic.mean()),
        "rank_ic": _safe_number(rank_ic.mean()),
        "icir": _safe_number(ic.mean() / ic.std(ddof=1)) if len(ic) > 1 and ic.std(ddof=1) else None,
        "rank_icir": _safe_number(rank_ic.mean() / rank_std) if rank_std else None,
        "quantile_returns": {
            "top": _safe_number(top_daily.mean()),
            "bottom": _safe_number(bottom_daily.mean()),
            "top_minus_bottom": _safe_number((top_daily - bottom_daily).mean()),
        },
        "top_quintile_turnover": _safe_number(
            sum(turnover_values) / len(turnover_values) if turnover_values else None
        ),
        "decay_rank_ic": decay,
        "subperiod_rank_ic": subperiod_rank_ic,
        "neutralization": definition.neutralization,
        "value_storage": "ON_DEMAND_FROM_IMMUTABLE_PARQUET;CONTROL_SQLITE_METRICS_ONLY",
    }
    report = {
        "status": "COMPLETED",
        "evaluation_id": evaluation_id,
        "factor_version_id": definition.factor_version_id,
        "definition": json.loads(str(registry_row["definition_json"])),
        "snapshot_hash": snapshot_hash,
        "oos_receipt_id": receipt.receipt_id,
        "oos_decision": receipt.decision.value,
        "metrics": metrics,
        "evidence": {
            "profile": PROFILE,
            "market_manifest_hash": manifest["manifest_hash"],
            "source_code_hash": registry_row["source_code_hash"],
            "parameter_hash": registry_row["parameter_hash"],
        },
        "synthetic": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    target = (
        cfg.paths.artifacts
        / "evidence"
        / "FACTOR_LAB_A_SHARE"
        / f"{evaluation_id}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, target)
    evidence_ref = str(target.relative_to(cfg.paths.artifacts.parent))
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO factor_evaluations VALUES (?,?,?,?,?,?,?,?)",
            (
                evaluation_id,
                definition.factor_version_id,
                snapshot_hash,
                receipt.receipt_id,
                "COMPLETED",
                json.dumps(metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                evidence_ref,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    report["evidence_ref"] = evidence_ref
    return report


__all__ = ["evaluate_large_price_factor"]
