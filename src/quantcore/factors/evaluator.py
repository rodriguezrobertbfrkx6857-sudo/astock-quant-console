from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from src.quantcore.contracts.config import V2Config
from src.quantcore.data.manifest import ManifestStore
from src.quantcore.factors.price import (
    FACTOR_CATALOG,
    FactorDefinition,
    compute_factor,
    factor_source_hash,
    safe_float,
)
from src.quantcore.research.oos import AccessDecision, AccessPurpose, OOSAccessService
from src.quantcore.research.registry import canonical_hash


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRICE_DATASET = "research_etf3_prices"
MIN_CROSS_SECTION = 5


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _load_prices(cfg: V2Config) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{(cfg.paths.state / 'control.sqlite').as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        row = conn.execute(
            "SELECT m.manifest_id,m.manifest_json FROM data_publications p "
            "JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE p.dataset=? AND m.layer='CURATED' AND m.provenance='REAL' "
            "AND m.readiness='REAL_READY'",
            (PRICE_DATASET,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"missing REAL_READY publication: {PRICE_DATASET}")
    store = ManifestStore(cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256")
    manifest = store.load(str(row[0]))
    records: list[dict[str, Any]] = []
    for digest in manifest.object_hashes:
        path = store.object_root / digest[:2] / digest
        records.extend(dict(x) for x in json.loads(path.read_text(encoding="utf-8")))
    return json.loads(str(row[1])), records


def _rank(values: list[Decimal]) -> list[Decimal]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = [Decimal(0)] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = Decimal(index + 1 + end) / Decimal(2)
        for position in range(index, end):
            result[ordered[position][0]] = rank
        index = end
    return result


def _pearson(left: list[Decimal], right: list[Decimal]) -> Decimal | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left, Decimal(0)) / Decimal(len(left))
    right_mean = sum(right, Decimal(0)) / Decimal(len(right))
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_var = sum(((a - left_mean) ** 2 for a in left), Decimal(0))
    right_var = sum(((b - right_mean) ** 2 for b in right), Decimal(0))
    if left_var == 0 or right_var == 0:
        return None
    return numerator / (left_var * right_var).sqrt()


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def _write_evidence(cfg: V2Config, evaluation_id: str, report: dict[str, Any]) -> str:
    target = cfg.paths.artifacts / "evidence" / "FACTOR_LAB" / f"{evaluation_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(target)
    return str(target.relative_to(cfg.paths.artifacts.parent))


def register_price_factors(cfg: V2Config) -> dict[str, Any]:
    source_hash = factor_source_hash()
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for definition in FACTOR_CATALOG.values():
            payload = {
                "factor_id": definition.factor_id,
                "version": definition.version,
                "family_id": definition.family_id,
                "direction": definition.direction,
                "units": definition.units,
                "lookback": definition.lookback,
                "inputs": list(definition.inputs),
                "available_at": definition.available_at,
                "universe": definition.universe,
                "missing_policy": definition.missing_policy,
                "winsorization": definition.winsorization,
                "normalization": definition.normalization,
                "neutralization": definition.neutralization,
                "cost_implication": definition.cost_implication,
                "role": definition.role,
                "evaluation_start": definition.evaluation_start,
                "evaluation_end": definition.evaluation_end,
            }
            values = (
                definition.factor_version_id,
                definition.factor_id,
                definition.version,
                definition.family_id,
                _json(payload),
                source_hash,
                definition.parameter_hash,
                _json({"ALL_OF": list(definition.inputs), "ANY_OF": [], "OPTIONAL": []}),
                definition.universe,
                "REGISTERED",
                datetime.now(UTC).isoformat(),
            )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO factor_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)", values
            )
            inserted += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": "PASS", "registered": len(FACTOR_CATALOG), "inserted": inserted, "source_code_hash": source_hash}


def _definition_from_row(row: sqlite3.Row) -> FactorDefinition:
    payload = json.loads(str(row["definition_json"]))
    return FactorDefinition(
        factor_version_id=str(row["factor_version_id"]),
        factor_id=str(payload["factor_id"]),
        version=str(payload["version"]),
        family_id=str(payload["family_id"]),
        direction=str(payload["direction"]),
        units=str(payload["units"]),
        lookback=int(payload["lookback"]),
        inputs=tuple(str(x) for x in payload["inputs"]),
        missing_policy=str(payload["missing_policy"]),
        winsorization=str(payload["winsorization"]),
        normalization=str(payload["normalization"]),
        neutralization=str(payload["neutralization"]),
        cost_implication=str(payload["cost_implication"]),
        role=str(payload["role"]),
        universe=str(payload["universe"]),
        available_at=str(payload["available_at"]),
        evaluation_start=str(payload["evaluation_start"]),
        evaluation_end=str(payload["evaluation_end"]),
    )


def evaluate_price_factor(cfg: V2Config, factor_version_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM factor_versions WHERE factor_version_id=?", (factor_version_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(factor_version_id)
    definition = _definition_from_row(row)
    if definition.universe == "A_SHARE_PRICE_ONLY_V1":
        from src.quantcore.factors.large_evaluator import evaluate_large_price_factor

        return evaluate_large_price_factor(cfg, definition, row)
    manifest, records = _load_prices(cfg)
    by_asset: dict[str, dict[date, Decimal]] = {}
    for item in records:
        code = str(item.get("ts_code") or "")
        if not code:
            continue
        by_asset.setdefault(code, {})[_parse_date(item["trade_date"])] = Decimal(str(item["close"]))
    assets = sorted(by_asset)
    sessions = sorted(set.intersection(*(set(values) for values in by_asset.values()))) if by_asset else []
    start = _parse_date(definition.evaluation_start)
    end = _parse_date(definition.evaluation_end)
    sessions = [x for x in sessions if start <= x <= end]
    if len(sessions) <= definition.lookback:
        raise RuntimeError("factor evaluation window does not contain a warm lookback")
    snapshot_hash = canonical_hash({
        "factor_version_id": factor_version_id,
        "manifest_hash": manifest["manifest_hash"],
        "assets": assets,
        "start": start.isoformat(),
        "end": end.isoformat(),
    })
    receipt = OOSAccessService(cfg.paths.state / "control.sqlite").authorize_known(
        protocol_hash=hashlib.sha256(f"factor:{factor_version_id}".encode()).hexdigest(),
        snapshot_hash=snapshot_hash,
        start=start,
        end=end,
        purpose=AccessPurpose.RESEARCH,
        asset_scope="ETF3",
        family_scope=definition.family_id,
    )
    if receipt.decision is not AccessDecision.ALLOW_KNOWN:
        return {
            "status": "BLOCKED_DATA",
            "factor_version_id": factor_version_id,
            "snapshot_hash": snapshot_hash,
            "oos_decision": receipt.decision.value,
            "reason": receipt.reason,
        }
    evaluation_id = f"factor-eval-{factor_version_id.replace('.', '-')}-{snapshot_hash[:20]}"
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT * FROM factor_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
    finally:
        conn.close()
    if existing is not None:
        return {
            "status": str(existing["status"]),
            "evaluation_id": evaluation_id,
            "factor_version_id": factor_version_id,
            "snapshot_hash": snapshot_hash,
            "metrics": json.loads(str(existing["metrics_json"])),
            "evidence_ref": existing["evidence_ref"],
            "idempotent": True,
        }

    observations: list[tuple[str, str, str, str, str, str, str, str]] = []
    by_date: dict[date, list[tuple[str, Decimal, Decimal]]] = {}
    for index, on in enumerate(sessions[:-1]):
        prior_sessions = sessions[: index + 1]
        if len(prior_sessions) < definition.lookback + 1:
            continue
        next_date = sessions[index + 1]
        for asset in assets:
            closes = [by_asset[asset][x] for x in prior_sessions]
            try:
                value = compute_factor(definition, closes)
            except ValueError:
                continue
            forward = by_asset[asset][next_date] / by_asset[asset][on] - Decimal(1)
            by_date.setdefault(on, []).append((asset, value, forward))
            observations.append(
                (
                    evaluation_id,
                    factor_version_id,
                    on.isoformat(),
                    asset,
                    format(value, "f"),
                    format(forward, "f"),
                    datetime.combine(on, time(17, 0), SHANGHAI).isoformat(),
                    str(manifest["manifest_hash"]),
                )
            )
    ics: list[Decimal] = []
    rank_ics: list[Decimal] = []
    top_returns: list[Decimal] = []
    bottom_returns: list[Decimal] = []
    for values in by_date.values():
        if len(values) < 2:
            continue
        factors = [item[1] for item in values]
        returns = [item[2] for item in values]
        ic = _pearson(factors, returns)
        rank_ic = _pearson(_rank(factors), _rank(returns))
        if ic is not None:
            ics.append(ic)
        if rank_ic is not None:
            rank_ics.append(rank_ic)
        ordered = sorted(values, key=lambda item: (item[1], item[0]))
        bottom_returns.append(ordered[0][2])
        top_returns.append(ordered[-1][2])
    coverage_dates = len(by_date)
    total_possible = max(1, len(sessions) - 1)
    top_mean = _mean(top_returns)
    bottom_mean = _mean(bottom_returns)
    metrics: dict[str, Any] = {
        "factor_id": definition.factor_id,
        "factor_version_id": factor_version_id,
        "input_manifest_hash": manifest["manifest_hash"],
        "assets": assets,
        "sample_dates": coverage_dates,
        "factor_observations": len(observations),
        "cross_section_count": len(assets),
        "min_cross_section_required": MIN_CROSS_SECTION,
        "coverage": coverage_dates / total_possible,
        "missing_ratio": 1 - (len(observations) / max(1, total_possible * len(assets))),
        "ic": safe_float(_mean(ics)),
        "rank_ic": safe_float(_mean(rank_ics)),
        "quantile_returns": {
            "top": safe_float(_mean(top_returns)),
            "bottom": safe_float(_mean(bottom_returns)),
            "top_minus_bottom": safe_float(top_mean - bottom_mean)
            if top_mean is not None and bottom_mean is not None
            else None,
        },
        "stability": {
            "subperiods": 3,
            "rank_ic_nonempty_dates": len(rank_ics),
            "regime_breakdown": "NOT_ESTIMATED_WITH_THREE_ASSET_CROSS_SECTION",
        },
    }
    status = "COMPLETED" if len(assets) >= MIN_CROSS_SECTION else "INSUFFICIENT_DATA"
    metrics["evaluation_reasons"] = [] if status == "COMPLETED" else [
        f"CROSS_SECTIONAL_N_{len(assets)}_BELOW_{MIN_CROSS_SECTION}",
        "THREE_ETF_RESULTS_MUST_NOT_BE_PRESENTED_AS_ALL_A_SHARE_IC",
    ]
    report = {
        "status": status,
        "evaluation_id": evaluation_id,
        "factor_version_id": factor_version_id,
        "definition": json.loads(str(row["definition_json"])),
        "snapshot_hash": snapshot_hash,
        "oos_receipt_id": receipt.receipt_id,
        "oos_decision": receipt.decision.value,
        "metrics": metrics,
        "evidence": {
            "dataset": PRICE_DATASET,
            "manifest_hash": manifest["manifest_hash"],
            "source_id": manifest.get("source_id"),
            "source_code_hash": row["source_code_hash"],
            "parameter_hash": row["parameter_hash"],
        },
        "synthetic": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    evidence_ref = _write_evidence(cfg, evaluation_id, report)
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO factor_evaluations VALUES (?,?,?,?,?,?,?,?)",
            (evaluation_id, factor_version_id, snapshot_hash, receipt.receipt_id, status, _json(metrics), evidence_ref, datetime.now(UTC).isoformat()),
        )
        conn.executemany(
            "INSERT INTO factor_values VALUES (?,?,?,?,?,?,?,?)", observations
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    report["evidence_ref"] = evidence_ref
    return report


__all__ = ["evaluate_price_factor", "register_price_factors"]
