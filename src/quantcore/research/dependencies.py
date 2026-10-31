"""Strategy-scoped data dependency resolution.

The queue stores logical dependencies (for example ``canonical_prices``),
while the canonical store contains concrete datasets.  This module is the
single translation layer between the two.  It deliberately checks the
published REAL_READY manifest coverage instead of treating the existence of
any row in the database as proof that a strategy is runnable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.quantcore.contracts.safety import PathBoundary


@dataclass(frozen=True)
class DependencyAssessment:
    """The immutable result of assessing one queue item."""

    ready: bool
    missing: tuple[str, ...]
    evidence: dict[str, dict[str, Any]]
    requested_start: str
    requested_end: str

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"MISSING_REAL_READY:{name}" for name in self.missing)


# This is a reporting contract, not a way to relax the frozen protocol.  It
# makes the dependency boundary visible to users and keeps unrelated domains
# from being presented as blockers for every strategy family.
STRATEGY_DEPENDENCY_SETS: dict[str, dict[str, tuple[str, ...]]] = {
    "LOW_VOL": {
        "REQUIRED": ("canonical_returns", "historical_universe"),
        "OPTIONAL": ("past_turnover", "benchmark_returns"),
        "UNUSED": ("event_text", "pit_financial_revisions", "cash_dividends"),
    },
    "MOMENTUM": {
        "REQUIRED": ("causal_total_returns", "historical_universe"),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "cash_dividends"),
    },
    "REVERSAL_LIQUIDITY": {
        "REQUIRED": (
            "unadjusted_execution_prices",
            "past_turnover",
            "historical_universe",
        ),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "cash_dividends"),
    },
    "TREND_DEFENSIVE": {
        "REQUIRED": ("index_signal_prices", "historical_universe"),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "cash_dividends"),
    },
    "LIQUIDITY": {
        "REQUIRED": ("past_turnover", "historical_universe"),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "cash_dividends"),
    },
    "ETF_ROTATION": {
        "REQUIRED": (
            "canonical_prices",
            "historical_etf_master",
            "execution_limits",
        ),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "historical_universe"),
    },
    "ETF_LOW_VOL": {
        "REQUIRED": (
            "canonical_prices",
            "historical_etf_master",
            "execution_limits",
        ),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "historical_universe"),
    },
    "ETF_TREND_DEFENSIVE": {
        "REQUIRED": (
            "canonical_prices",
            "historical_etf_master",
            "execution_limits",
        ),
        "OPTIONAL": ("benchmark_returns",),
        "UNUSED": ("event_text", "pit_financial_revisions", "historical_universe"),
    },
}


def dependency_set(protocol: dict[str, Any]) -> dict[str, list[str]]:
    """Return the visible REQUIRED/OPTIONAL/UNUSED contract for a protocol."""

    family = str(protocol.get("family_id", ""))
    known = STRATEGY_DEPENDENCY_SETS.get(family)
    if known is not None:
        required = list(known["REQUIRED"])
        optional = list(known["OPTIONAL"])
        return {
            **{key: list(value) for key, value in known.items()},
            # New names make the contract explicit while REQUIRED/UNUSED stay
            # for compatibility with the existing API and UI.
            "ALL_OF": required,
            "ANY_OF": [],
            "OPTIONAL": optional,
        }
    required = [str(item) for item in protocol.get("data_dependencies", [])]
    return {
        "REQUIRED": list(required),
        "ALL_OF": list(required),
        "ANY_OF": [],
        "OPTIONAL": [],
        "UNUSED": [],
    }


def _as_date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    return date.fromisoformat(text)


def _protocol_range(protocol: dict[str, Any]) -> tuple[date, date]:
    train = protocol.get("train_window") or ["1900-01-01", "1900-01-01"]
    windows = protocol.get("validation_windows") or []
    starts = [_as_date(train[0])]
    ends = [_as_date(train[1])]
    for window in windows:
        starts.append(_as_date(window[0]))
        ends.append(_as_date(window[1]))
    return min(starts), max(ends)


def _published(database: Path) -> dict[str, dict[str, Any]]:
    database = PathBoundary().require_readable(database)
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT p.dataset,m.manifest_id,m.manifest_hash,m.manifest_json "
            "FROM data_publications p JOIN data_manifests m ON m.manifest_id=p.manifest_id "
            "WHERE m.layer='CURATED' AND m.provenance='REAL' "
            "AND m.readiness='REAL_READY'"
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        manifest = json.loads(str(row["manifest_json"]))
        manifest["manifest_id"] = row["manifest_id"]
        manifest["manifest_hash"] = row["manifest_hash"]
        result[str(row["dataset"])] = manifest
    return result


def _covers(manifest: dict[str, Any], start: date, end: date) -> bool:
    """Check interval intersection without treating missing dates as bars.

    A dataset may begin on the first exchange session after a calendar range
    starts, so only the upper and lower interval bounds are checked here.
    Exact session completeness is checked by research executors before a run.
    """

    raw_start = manifest.get("start_date")
    raw_end = manifest.get("end_date")
    if raw_start in (None, "None") or raw_end in (None, "None"):
        return True
    try:
        return _as_date(raw_start) <= end and _as_date(raw_end) >= start
    except (TypeError, ValueError):
        return False


def _state_evidence(database: Path, *, security_type: str | None = None) -> dict[str, Any] | None:
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        if security_type is None:
            row = conn.execute(
                "SELECT count(*) AS n FROM security_state_versions"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT count(*) AS n FROM security_state_versions s "
                "JOIN security_identities i ON i.asset_id=s.asset_id "
                "WHERE i.security_type=?",
                (security_type,),
            ).fetchone()
    finally:
        conn.close()
    count = int(row[0]) if row else 0
    return {"kind": "database", "state_rows": count} if count else None


def _requirements(protocol: dict[str, Any]) -> dict[str, tuple[str, ...] | None]:
    """Resolve logical dependencies to concrete alternatives.

    ``None`` means a database contract rather than a manifest.  ETF Rotation
    has an implicit execution-limit requirement because its frozen execution
    rule cannot be validated from prices alone.
    """

    family = str(protocol.get("family_id", ""))
    result: dict[str, tuple[str, ...] | None] = {}
    for dependency in protocol.get("data_dependencies", []):
        name = str(dependency)
        result[name] = {
            "canonical_prices": ("research_etf3_prices", "fund_daily", "daily"),
            "canonical_returns": ("daily", "adj_factor"),
            "causal_total_returns": ("daily", "adj_factor"),
            "unadjusted_execution_prices": ("daily",),
            "past_turnover": ("daily_basic",),
            "index_signal_prices": ("index_daily",),
            "tradable_etf_prices": ("research_etf3_prices", "fund_daily"),
            "benchmark_returns": ("index_daily",),
            "pit_valuation": ("daily_basic",),
            "cash_dividends": ("dividend",),
            "pit_financial_revisions": ("fina_indicator",),
            "event_text": ("anns_d",),
            "first_visible_at": ("anns_d",),
            "security_mapping": ("anns_d", "historical_etf_master"),
            "historical_industry": ("historical_industry",),
            "historical_market_cap": ("historical_market_cap",),
            "historical_etf_master": ("historical_etf_master",),
        }.get(name)
    if family in {"ETF_ROTATION", "ETF_LOW_VOL", "ETF_TREND_DEFENSIVE"}:
        result.setdefault("execution_limits", ("research_etf3_limits", "known_etf3_limits"))
    if family in {
        "LOW_VOL",
        "MOMENTUM",
        "REVERSAL_LIQUIDITY",
        "LIQUIDITY",
        "TREND_DEFENSIVE",
    }:
        # A stock price strategy requires verified historical stock states;
        # current stock_basic is not enough and does not satisfy this gate.
        result.setdefault("historical_universe", None)
    return result


def assess_protocol(
    database: str | Path,
    protocol: dict[str, Any],
) -> DependencyAssessment:
    """Return a precise, fail-closed assessment for one frozen protocol."""

    database = PathBoundary().require_readable(database)
    start, end = _protocol_range(protocol)
    published = _published(database)
    evidence: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for logical, alternatives in _requirements(protocol).items():
        selected: tuple[str, dict[str, Any]] | None = None
        if alternatives is None:
            selected_state = _state_evidence(
                database,
                security_type=(
                    "STOCK"
                    if logical == "historical_universe"
                    else None
                ),
            )
            if selected_state is not None:
                selected = ("control.sqlite", selected_state)
        else:
            for dataset in alternatives:
                manifest = published.get(dataset)
                if manifest is None or not _covers(manifest, start, end):
                    continue
                # Execution limits are not an optional convenience.  A
                # backfill that starts after the frozen train window cannot
                # be used to silently skip the missing early sessions.
                if dataset in {"research_etf3_limits", "known_etf3_limits"}:
                    raw_start = manifest.get("start_date")
                    raw_end = manifest.get("end_date")
                    if raw_start in (None, "None") or raw_end in (None, "None"):
                        continue
                    if _as_date(raw_start) > start or _as_date(raw_end) < end:
                        continue
                selected = (
                    dataset,
                    {
                        "manifest_id": manifest.get("manifest_id"),
                        "manifest_hash": manifest.get("manifest_hash"),
                        "row_count": manifest.get("row_count"),
                        "start_date": manifest.get("start_date"),
                        "end_date": manifest.get("end_date"),
                        "source_id": manifest.get("source_id"),
                    },
                )
                break
        if selected is None:
            missing.append(logical)
        else:
            evidence[logical] = {"dataset": selected[0], **selected[1]}
    return DependencyAssessment(
        not missing,
        tuple(sorted(set(missing))),
        evidence,
        start.isoformat(),
        end.isoformat(),
    )


def assess_queue(
    database: str | Path,
) -> tuple[dict[str, object], ...]:
    """Assess all persisted queue items and return serializable results."""

    database = PathBoundary().require_writable(database)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT q.queue_item_id,q.status,p.protocol_json "
            "FROM research_queue q JOIN research_protocols p "
            "ON p.protocol_hash=q.protocol_hash ORDER BY q.priority"
        ).fetchall()
    finally:
        conn.close()
    results: list[dict[str, object]] = []
    for row in rows:
        protocol = json.loads(str(row["protocol_json"]))
        assessment = assess_protocol(database, protocol)
        results.append(
            {
                "queue_item_id": str(row["queue_item_id"]),
                "previous_status": str(row["status"]),
                "status": "READY" if assessment.ready else "BLOCKED_DATA",
                "reasons": list(assessment.reasons),
                "manifest_hashes": {
                    key: value.get("manifest_hash")
                    for key, value in assessment.evidence.items()
                    if value.get("manifest_hash")
                },
                "evidence": assessment.evidence,
                "dependency_set": dependency_set(protocol),
                "requested_start": assessment.requested_start,
                "requested_end": assessment.requested_end,
            }
        )
    return tuple(results)
