from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from src.quantcore.activation.vendor import RelayClient, VendorFailure
from src.quantcore.contracts.config import V2Config
from src.quantcore.data.hashing import canonical_json_bytes
from src.quantcore.data.manifest import ManifestStore, derive_readiness
from src.quantcore.data.models import DataLayer, DataManifest, Provenance
from src.quantcore.data.publish import PublicationStore
from src.quantcore.data.quality import evaluate_records
from src.quantcore.data.raw import RawStore


@dataclass(frozen=True)
class DatasetSpec:
    api: str
    params: dict[str, object]
    required: tuple[str, ...]
    unique: tuple[str, ...]
    available_at_policy: str


CORE_DATASETS: dict[str, DatasetSpec] = {
    "trade_cal": DatasetSpec(
        "trade_cal",
        {"exchange": "SSE", "start_date": "19900101"},
        ("cal_date", "is_open"),
        ("exchange", "cal_date"),
        "vendor_published_at_ingestion",
    ),
    "stock_basic": DatasetSpec(
        "stock_basic",
        {"list_status": "L"},
        ("ts_code", "list_date"),
        ("ts_code",),
        "vendor_published_at_ingestion",
    ),
    "daily": DatasetSpec(
        "daily",
        {},
        ("ts_code", "trade_date", "close"),
        ("ts_code", "trade_date"),
        "session_close_plus_vendor_publication",
    ),
    "adj_factor": DatasetSpec(
        "adj_factor",
        {},
        ("ts_code", "trade_date", "adj_factor"),
        ("ts_code", "trade_date"),
        "vendor_publication_time",
    ),
    "daily_basic": DatasetSpec(
        "daily_basic",
        {},
        ("ts_code", "trade_date"),
        ("ts_code", "trade_date"),
        "vendor_publication_time",
    ),
    "stk_limit": DatasetSpec(
        "stk_limit",
        {},
        ("ts_code", "trade_date"),
        ("ts_code", "trade_date"),
        "vendor_publication_time",
    ),
    "suspend_d": DatasetSpec(
        "suspend_d",
        {},
        ("ts_code", "trade_date"),
        ("ts_code", "trade_date"),
        "vendor_publication_time",
    ),
    "namechange": DatasetSpec(
        "namechange",
        {},
        ("ts_code", "start_date"),
        ("ts_code", "start_date", "name"),
        "announcement_time_or_ingestion_if_absent",
    ),
    "fund_basic": DatasetSpec(
        "fund_basic",
        {"market": "E"},
        ("ts_code",),
        ("ts_code",),
        "vendor_published_at_ingestion",
    ),
    "fund_daily": DatasetSpec(
        "fund_daily",
        {},
        ("ts_code", "trade_date", "close"),
        ("ts_code", "trade_date"),
        "session_close_plus_vendor_publication",
    ),
    "index_daily": DatasetSpec(
        "index_daily",
        {},
        ("ts_code", "trade_date", "close"),
        ("ts_code", "trade_date"),
        "session_close_plus_vendor_publication",
    ),
    "index_weight": DatasetSpec(
        "index_weight",
        {},
        ("index_code", "con_code", "trade_date"),
        ("index_code", "con_code", "trade_date"),
        "vendor_publication_time",
    ),
    "fina_indicator": DatasetSpec(
        "fina_indicator",
        {},
        ("ts_code", "end_date", "ann_date"),
        ("ts_code", "end_date", "ann_date"),
        "ann_date_not_end_date",
    ),
    "anns_d": DatasetSpec(
        "anns_d", {}, ("ann_date",), ("ann_date", "title"), "vendor_first_visible_time"
    ),
}


def deduplicate_identical(
    rows: list[dict[str, object]], unique_fields: tuple[str, ...]
) -> tuple[list[dict[str, object]], int]:
    """Collapse only fully identical rows sharing a business key.

    Conflicting rows remain duplicated so the quality gate fails. Immutable RAW
    pages are never modified.
    """
    seen: dict[tuple[object, ...], bytes] = {}
    normalized: list[dict[str, object]] = []
    removed = 0
    for row in rows:
        key = tuple(row.get(field) for field in unique_fields)
        body = canonical_json_bytes(row)
        previous = seen.get(key)
        if previous == body:
            removed += 1
            continue
        if previous is None:
            seen[key] = body
        normalized.append(row)
    return normalized, removed


def normalize_optional_fields(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Make schema-drifted missing optional fields explicit nulls."""
    fields = sorted(set().union(*(row.keys() for row in rows))) if rows else []
    return [{field: row.get(field) for field in fields} for row in rows]


def _published_symbol_set(
    raw: RawStore, manifest_payload: dict[str, object]
) -> set[str] | None:
    """Load the prior curated symbol cross-section when it is locally verifiable.

    The row-count anomaly is intentionally not used as a completeness gate.
    A publication can only advance when the prior curated object is available
    locally and its actual symbol set can be compared with the candidate.  A
    missing object is an evidence failure, not permission to publish.
    """
    hashes = manifest_payload.get("object_hashes")
    if not isinstance(hashes, list) or not hashes:
        return None
    try:
        rows: list[dict[str, object]] = []
        for digest in hashes:
            if not isinstance(digest, str):
                return None
            payload = json.loads(raw.read_object(digest))
            if not isinstance(payload, list):
                return None
            rows.extend(dict(item) for item in payload if isinstance(item, dict))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    symbols = {
        str(row.get("ts_code") or row.get("con_code") or "").strip()
        for row in rows
    }
    symbols.discard("")
    return symbols or None


def _status(
    cfg: V2Config, component: str, status: str, detail: str, evidence: str | None = None
) -> None:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT INTO operational_status VALUES (?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
            "status=excluded.status,detail=excluded.detail,evidence_ref=excluded.evidence_ref,updated_at=excluded.updated_at",
            (component, status, detail, evidence, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_attempt(cfg: V2Config, attempt) -> None:
    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO raw_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt.attempt_id,
                attempt.source_id,
                attempt.dataset,
                attempt.requested_at,
                attempt.completed_at,
                attempt.provenance.value,
                attempt.status.value,
                attempt.pages_expected,
                attempt.pages_received,
                attempt.row_count,
                json.dumps(attempt.request, ensure_ascii=False, sort_keys=True),
                json.dumps(attempt.object_hashes),
                attempt.error_code,
                attempt.error_message,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def sync_dataset(
    cfg: V2Config,
    dataset: str,
    *,
    business_date: str | None = None,
    preloaded_pages: list[list[dict[str, object]]] | None = None,
    source_id: str = "tushare-relay",
    request_override: dict[str, object] | None = None,
    spec_override: DatasetSpec | None = None,
    publish: bool = True,
    client_override: RelayClient | None = None,
) -> dict[str, object]:
    if dataset not in CORE_DATASETS and (preloaded_pages is None or spec_override is None):
        raise ValueError("non-core datasets require preloaded pages and an explicit spec")
    spec = spec_override or CORE_DATASETS[dataset]
    raw = RawStore(cfg.paths.data / "raw")
    request_params = dict(spec.params)
    if business_date and dataset not in {
        "trade_cal",
        "stock_basic",
        "fund_basic",
        "namechange",
    }:
        request_params["trade_date"] = business_date.replace("-", "")
    if dataset == "trade_cal":
        request_params["end_date"] = (
            business_date or datetime.now(UTC).date().isoformat()
        ).replace("-", "")
    if request_override is not None:
        request_params = dict(request_override)
    client = client_override or RelayClient()
    pagination_evidence: dict[str, object] = {}
    attempt_request = dict(request_params)
    try:
        if preloaded_pages is None:
            pages, complete = client.fetch_pages(spec.api, request_params)
            pagination_evidence = dict(getattr(client, "last_pagination", {}))
        else:
            pages, complete = preloaded_pages, True
            pagination_evidence = {
                "api": spec.api,
                "page_size": max((len(page) for page in pages), default=0),
                "max_pages": len(pages),
                "offsets": [sum(len(item) for item in pages[:index]) for index in range(len(pages))],
                "page_lengths": [len(page) for page in pages],
                "pages_received": len(pages),
                "complete": True,
                "termination": "PRELOADED_LOCAL_EVIDENCE",
                "total_hint": sum(len(page) for page in pages),
                "exact_page_limit": False,
                "offset_termination_verified": True,
                "boundary_review": "NOT_REQUIRED",
            }
        # Request parameters are retained for reproducibility.  Pagination
        # evidence contains only counts/offsets and is never a credential or a
        # response body.
        attempt_request["_pagination_evidence"] = pagination_evidence
        attempt = raw.capture_json_pages(
            source_id=source_id,
            dataset=dataset,
            request=attempt_request,
            pages=pages,
            provenance=Provenance.REAL,
            pages_expected=len(pages) if complete else len(pages) + 1,
        )
    except VendorFailure as exc:
        attempt_request["_pagination_evidence"] = {
            "complete": False,
            "termination": f"VENDOR_FAILURE:{exc.code}",
            "pages_received": 0,
            "page_lengths": [],
        }
        attempt = raw.capture_json_pages(
            source_id=source_id,
            dataset=dataset,
            request=attempt_request,
            pages=[],
            provenance=Provenance.REAL,
            pages_expected=1,
            error_code=exc.code,
            error_message="relay request failed; credential and response body are not persisted",
        )
    _persist_attempt(cfg, attempt)
    if attempt.status.value != "SUCCESS":
        state = (
            "PENDING_VENDOR"
            if attempt.error_code == "VENDOR_AUTH_REQUIRED"
            else "BLOCKED_DATA"
        )
        _status(
            cfg,
            f"data:{dataset}",
            state,
            attempt.error_code or attempt.status.value,
            f"attempt:{attempt.attempt_id}",
        )
        return {
            "dataset": dataset,
            "status": state,
            "attempt_id": attempt.attempt_id,
            "rows": 0,
        }

    raw_rows: list[dict[str, object]] = []
    for digest in attempt.object_hashes:
        page = json.loads(raw.read_object(digest))
        raw_rows.extend(dict(item) for item in page)
    rows, identical_duplicates = deduplicate_identical(
        normalize_optional_fields(raw_rows), spec.unique
    )
    quality = evaluate_records(
        rows,
        required_fields=spec.required,
        unique_fields=spec.unique,
        policy_version="activation.v1",
    )
    normalized_hash = raw.put_object(canonical_json_bytes(rows))
    timestamp = datetime.now(UTC)
    version = timestamp.strftime("v%Y%m%dT%H%M%S%fZ")
    manifests = ManifestStore(cfg.paths.data / "manifests", raw.objects)
    frozen: list[DataManifest] = []
    coverage_warnings: list[str] = []
    upstream: tuple[str, ...] = ()
    for layer, hashes, layer_rows in (
        (DataLayer.RAW, attempt.object_hashes, raw_rows),
        (DataLayer.NORMALIZED, (normalized_hash,), rows),
        (DataLayer.CURATED, (normalized_hash,), rows),
    ):
        readiness, reasons = derive_readiness(
            provenance=Provenance.REAL,
            attempt_status=attempt.status,
            row_count=len(layer_rows),
            pagination_complete=attempt.pages_expected == attempt.pages_received,
            quality_status=quality.status,
        )
        manifest = manifests.freeze(
            DataManifest(
                manifest_id=f"manifest-{uuid4().hex}",
                dataset=dataset,
                version=version,
                layer=layer,
                provenance=Provenance.REAL,
                source_id=attempt.source_id,
                created_at=timestamp.isoformat(),
                available_at_policy=spec.available_at_policy,
                schema_version=f"{dataset}.v1",
                normalizer_version="identity-strict.v1"
                if layer is not DataLayer.RAW
                else None,
                build_hash="integrated-v2-full-activation",
                object_hashes=hashes,
                upstream_manifest_ids=upstream,
                row_count=len(layer_rows),
                symbol_count=len(
                    {
                        str(x.get("ts_code") or x.get("con_code") or "")
                        for x in layer_rows
                    }
                ),
                start_date=min(
                    (
                        str(
                            x.get("trade_date")
                            or x.get("cal_date")
                            or x.get("ann_date")
                        )
                        for x in layer_rows
                    ),
                    default=None,
                ),
                end_date=max(
                    (
                        str(
                            x.get("trade_date")
                            or x.get("cal_date")
                            or x.get("ann_date")
                        )
                        for x in layer_rows
                    ),
                    default=None,
                ),
                pages_expected=attempt.pages_expected,
                pages_received=attempt.pages_received,
                attempt_status=attempt.status,
                quality_report_id=quality.report_id,
                quality_status=quality.status,
                readiness=readiness,
                reasons=reasons
                + (
                    (f"IDENTICAL_SOURCE_DUPLICATES_REMOVED:{identical_duplicates}",)
                    if layer is not DataLayer.RAW and identical_duplicates
                    else ()
                ),
            )
        )
        frozen.append(manifest)
        upstream = (manifest.manifest_id,)

    publish_allowed = publish
    publication_gate_reason: str | None = None
    if publish and dataset in {"daily", "adj_factor", "stk_limit"}:
        gate_conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
        try:
            current_row = gate_conn.execute(
                "SELECT m.manifest_json FROM data_publications p "
                "JOIN data_manifests m ON m.manifest_id=p.manifest_id WHERE p.dataset=?",
                (dataset,),
            ).fetchone()
        finally:
            gate_conn.close()
        if current_row is not None:
            current = json.loads(str(current_row[0]))
            current_start = str(current.get("start_date") or "")
            current_end = str(current.get("end_date") or "")
            candidate_start = str(frozen[-1].start_date or "")
            candidate_end = str(frozen[-1].end_date or "")
            current_rows = int(current.get("row_count") or 0)
            if candidate_end and current_end and candidate_end < current_end:
                publish_allowed = False
                publication_gate_reason = (
                    f"STALE_PUBLICATION_CANDIDATE:candidate={candidate_end};current={current_end}"
                )
            elif (
                current_start == current_end
                and candidate_start == candidate_end
                and candidate_end >= current_end
                and current_rows > 0
                and len(rows) < int(current_rows * 0.95)
            ):
                # A previous-session row ratio is an anomaly signal only.  It
                # cannot approve or reject a new session by itself; the actual
                # gate is schema/quality/pagination evidence.
                coverage_warnings.append(
                    "ROW_COUNT_BELOW_95PCT_PREVIOUS_SESSION:"
                    f"rows={len(rows)};previous={current_rows}"
                )
            if (
                candidate_end
                and current_end
                and candidate_end > current_end
                and candidate_start == candidate_end
            ):
                prior_symbols = _published_symbol_set(raw, current)
                candidate_symbols = {
                    str(row.get("ts_code") or row.get("con_code") or "").strip()
                    for row in rows
                }
                candidate_symbols.discard("")
                if prior_symbols is None:
                    publish_allowed = False
                    publication_gate_reason = (
                        "CROSS_SESSION_COMPLETENESS_FAILED:"
                        "prior_published_symbol_set_unavailable"
                    )
                else:
                    missing_symbols = prior_symbols - candidate_symbols
                    if missing_symbols:
                        publish_allowed = False
                        publication_gate_reason = (
                            "CROSS_SESSION_COMPLETENESS_FAILED:"
                            f"missing_prior_symbols={len(missing_symbols)};"
                            f"prior_symbols={len(prior_symbols)};"
                            f"candidate_symbols={len(candidate_symbols)}"
                        )
        if (
            pagination_evidence.get("exact_page_limit") is True
            and pagination_evidence.get("offset_termination_verified") is not True
        ):
            publish_allowed = False
            publication_gate_reason = (
                "PAGINATION_BOUNDARY_UNVERIFIED:initial_page_exactly_equals_request_limit"
            )

    conn = sqlite3.connect(cfg.paths.state / "control.sqlite")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for manifest in frozen:
            conn.execute(
                "INSERT INTO data_manifests VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_id,
                    manifest.dataset,
                    manifest.version,
                    manifest.layer.value,
                    manifest.provenance.value,
                    manifest.readiness.value,
                    manifest.manifest_hash,
                    json.dumps(
                        {**manifest.payload(), "manifest_hash": manifest.manifest_hash},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    manifest.created_at,
                ),
            )
        curated = frozen[-1]
        conn.execute(
            "INSERT INTO quality_reports VALUES (?,?,?,?,?,?,?)",
            (
                quality.report_id,
                curated.manifest_id,
                quality.status.value,
                quality.input_hash,
                quality.policy_version,
                json.dumps(quality.to_dict(), ensure_ascii=False, sort_keys=True),
                timestamp.isoformat(),
            ),
        )
        if curated.readiness.value == "REAL_READY" and publish_allowed:
            conn.execute(
                "INSERT INTO data_publications VALUES (?,?,?,?) ON CONFLICT(dataset) DO UPDATE SET "
                "manifest_id=excluded.manifest_id,manifest_hash=excluded.manifest_hash,published_at=excluded.published_at",
                (
                    dataset,
                    curated.manifest_id,
                    curated.manifest_hash,
                    timestamp.isoformat(),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if frozen[-1].readiness.value == "REAL_READY" and publish_allowed:
        PublicationStore(cfg.paths.data / "published", manifests).publish(
            dataset, frozen[-1].manifest_id
        )
        _status(
            cfg,
            f"data:{dataset}",
            "PASS",
            f"REAL_READY rows={len(rows)}"
            + (";warnings=" + ",".join(coverage_warnings) if coverage_warnings else ""),
            f"manifest:{frozen[-1].manifest_hash}",
        )
    elif frozen[-1].readiness.value == "REAL_READY" and not publish:
        _status(
            cfg,
            f"data_history:{dataset}",
            "PASS",
            f"REAL_READY rows={len(rows)}; publication pointer unchanged",
            f"manifest:{frozen[-1].manifest_hash}",
        )
    elif publication_gate_reason:
        _status(
            cfg,
            f"data:{dataset}",
            "BLOCKED_DATA",
            publication_gate_reason,
            f"manifest:{frozen[-1].manifest_hash}",
        )
    else:
        _status(
            cfg,
            f"data:{dataset}",
            "BLOCKED_DATA",
            ",".join(frozen[-1].reasons),
            f"manifest:{frozen[-1].manifest_hash}",
        )
    return {
        "dataset": dataset,
        "status": "BLOCKED_DATA" if publication_gate_reason else frozen[-1].readiness.value,
        "rows": len(rows),
        "manifest_hash": frozen[-1].manifest_hash,
        "publication_updated": bool(
            frozen[-1].readiness.value == "REAL_READY" and publish_allowed
        ),
        "publication_gate_reason": publication_gate_reason,
        "coverage_warnings": coverage_warnings,
        "pagination_evidence": pagination_evidence,
    }


def sync_core(
    cfg: V2Config,
    *,
    business_date: str | None = None,
    datasets: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    selected = datasets or tuple(CORE_DATASETS)
    return [sync_dataset(cfg, name, business_date=business_date) for name in selected]
