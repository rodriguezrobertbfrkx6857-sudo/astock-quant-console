"""Public Sina/Tencent EOD supplement published through the V2 data contract.

This module never opens or mutates the local Legacy evidence archive.  It
captures only fields explicitly supplied by the public endpoints.  Missing
execution, adjustment and security-state fields remain null, so this dataset
is display-only PRICE_ONLY evidence and cannot authorize research or orders.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

import requests  # type: ignore[import-untyped]

from src.quantcore.activation.data_worker import DatasetSpec, sync_dataset
from src.quantcore.contracts.config import V2Config
from src.quantcore.data.manifest import ManifestStore
from src.quantcore.data.raw import RawStore


DATASET = "daily_price_only"
SOURCE_ID = "public-eod-sina-tencent"
SPEC = DatasetSpec(
    api="public_eod",
    params={},
    required=("ts_code", "trade_date", "open", "high", "low", "close"),
    unique=("ts_code", "trade_date"),
    available_at_policy="session_close_plus_public_endpoint_observation",
)
_CODE = re.compile(r"(?:0|3|6)\d{5}\.(?:SZ|SH)")
_TENCENT_QUOTE = re.compile(r'v_(sh|sz)(\d{6})="([^"]*)";')


class PublicEODError(RuntimeError):
    pass


def _number(value: object) -> float:
    number = float(str(value))
    if not (number > 0):
        raise ValueError("price/volume value must be positive")
    return number


def _row(
    *,
    ts_code: str,
    trade_date: str,
    open_value: object,
    high_value: object,
    low_value: object,
    close_value: object,
    volume_value: object,
    pre_close: float | None,
    provider: str,
    volume_unit: str,
    fragment: object,
) -> dict[str, object]:
    open_price = _number(open_value)
    high = _number(high_value)
    low = _number(low_value)
    close = _number(close_value)
    volume = _number(volume_value)
    if low > min(open_price, close) or high < max(open_price, close) or low > high:
        raise ValueError("invalid OHLC relationship")
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "vol": volume,
        "volume_unit": volume_unit,
        "amount": None,
        "adj_factor": None,
        "up_limit": None,
        "down_limit": None,
        "is_suspended": None,
        "is_st": None,
        "board": None,
        "source_provider": provider,
        "source_fragment_sha256": hashlib.sha256(
            json.dumps(fragment, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "price_basis": "UNADJUSTED_PRICE_ONLY",
        "available_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T18:00:00+08:00",
        "execution_eligible": False,
        "research_authorized": False,
    }


def normalize_tencent_payload(
    payload: dict[str, Any], ts_code: str, allowed_dates: set[str]
) -> list[dict[str, object]]:
    """Accept Tencent's unadjusted ``day`` field only; never fall back to qfqday."""
    symbol = ("sh" if ts_code.endswith(".SH") else "sz") + ts_code[:6]
    item = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = item.get("day")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise PublicEODError("Tencent unadjusted day series is absent")
    parsed: list[tuple[str, list[Any]]] = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        date = str(raw[0]).replace("-", "")
        if len(date) == 8 and date.isdigit():
            parsed.append((date, raw))
    parsed.sort(key=lambda value: value[0])
    result: list[dict[str, object]] = []
    previous_close: float | None = None
    for date, raw in parsed:
        close = _number(raw[2])
        if date in allowed_dates:
            result.append(
                _row(
                    ts_code=ts_code,
                    trade_date=date,
                    open_value=raw[1],
                    high_value=raw[3],
                    low_value=raw[4],
                    close_value=raw[2],
                    volume_value=raw[5],
                    pre_close=previous_close,
                    provider="TENCENT_PUBLIC_DAY",
                    volume_unit="LOT_100_SHARES",
                    fragment=raw,
                )
            )
        previous_close = close
    return result


def normalize_sina_payload(
    payload: list[dict[str, Any]], ts_code: str, allowed_dates: set[str]
) -> list[dict[str, object]]:
    if not isinstance(payload, list) or not payload:
        raise PublicEODError("Sina daily series is absent")
    parsed = sorted(
        (
            (str(raw.get("day") or "").replace("-", ""), raw)
            for raw in payload
            if isinstance(raw, dict)
        ),
        key=lambda value: value[0],
    )
    result: list[dict[str, object]] = []
    previous_close: float | None = None
    for date, raw in parsed:
        if len(date) != 8 or not date.isdigit():
            continue
        close = _number(raw.get("close"))
        if date in allowed_dates:
            result.append(
                _row(
                    ts_code=ts_code,
                    trade_date=date,
                    open_value=raw.get("open"),
                    high_value=raw.get("high"),
                    low_value=raw.get("low"),
                    close_value=raw.get("close"),
                    volume_value=raw.get("volume"),
                    pre_close=previous_close,
                    provider="SINA_PUBLIC_KLINE_240",
                    volume_unit="SHARE",
                    fragment=raw,
                )
            )
        previous_close = close
    return result


def normalize_tencent_quote_payload(
    payload: str, target_date: str
) -> dict[str, list[dict[str, object]]]:
    """Normalize Tencent's batch quote response for one completed session.

    Field 36 is reported in lots (100 shares).  Amount is intentionally left
    null because the public response's display-unit convention is not a
    sufficiently strong contract for canonical use.
    """
    result: dict[str, list[dict[str, object]]] = {}
    for match in _TENCENT_QUOTE.finditer(payload):
        exchange, symbol, body = match.groups()
        fields = body.split("~")
        if len(fields) <= 36:
            continue
        observed = fields[30][:8]
        if observed != target_date:
            continue
        ts_code = f"{symbol}.{'SH' if exchange == 'sh' else 'SZ'}"
        try:
            pre_close = _number(fields[4])
            row = _row(
                ts_code=ts_code,
                trade_date=observed,
                open_value=fields[5],
                high_value=fields[33],
                low_value=fields[34],
                close_value=fields[3],
                volume_value=fields[36],
                pre_close=pre_close,
                provider="TENCENT_PUBLIC_BATCH_QUOTE",
                volume_unit="LOT_100_SHARES",
                fragment={"symbol": symbol, "fields": fields[3:6] + fields[30:37]},
            )
        except (TypeError, ValueError):
            continue
        result[ts_code] = [row]
    return result


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _fetch_tencent_quote_batch(
    symbols: list[str], target_date: str
) -> dict[str, list[dict[str, object]]]:
    query = ",".join(
        ("sh" if code.endswith(".SH") else "sz") + code[:6] for code in symbols
    )
    response = requests.get(
        "https://qt.gtimg.cn/q=" + query,
        timeout=15,
    )
    response.raise_for_status()
    return normalize_tencent_quote_payload(
        response.content.decode("gbk", errors="replace"), target_date
    )


def _fetch_tencent(ts_code: str, datalen: int, allowed_dates: set[str]) -> list[dict[str, object]]:
    symbol = ("sh" if ts_code.endswith(".SH") else "sz") + ts_code[:6]
    response = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,,,{datalen},"},
        timeout=12,
    )
    response.raise_for_status()
    return normalize_tencent_payload(response.json(), ts_code, allowed_dates)


def _fetch_sina(ts_code: str, datalen: int, allowed_dates: set[str]) -> list[dict[str, object]]:
    symbol = ("sh" if ts_code.endswith(".SH") else "sz") + ts_code[:6]
    response = requests.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": datalen},
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=12,
    )
    response.raise_for_status()
    return normalize_sina_payload(response.json(), ts_code, allowed_dates)


def _published_rows(cfg: V2Config, dataset: str) -> tuple[dict[str, Any], list[dict[str, object]]]:
    pointer = cfg.paths.data / "published" / f"{dataset}.json"
    if not pointer.is_file():
        return {}, []
    reference = json.loads(pointer.read_text("utf-8"))
    store = ManifestStore(
        cfg.paths.data / "manifests", cfg.paths.data / "raw" / "objects" / "sha256"
    )
    manifest = store.load(str(reference["manifest_id"]))
    raw = RawStore(cfg.paths.data / "raw")
    rows: list[dict[str, object]] = []
    for digest in manifest.object_hashes:
        value = json.loads(raw.read_object(digest))
        if not isinstance(value, list):
            raise PublicEODError(f"{dataset} publication object is not a row list")
        rows.extend(dict(item) for item in value if isinstance(item, dict))
    return {**reference, **manifest.payload(), "manifest_hash": manifest.manifest_hash}, rows


def _open_sessions(cfg: V2Config, target_date: str, count: int) -> list[str]:
    _, rows = _published_rows(cfg, "trade_cal")
    sessions = sorted(
        {
            str(row.get("cal_date") or "")
            for row in rows
            if str(row.get("is_open")) in {"1", "1.0", "True", "true"}
            and str(row.get("cal_date") or "") <= target_date
        }
    )
    if not sessions or sessions[-1] != target_date:
        raise PublicEODError("target is not the latest certified open session")
    return sessions[-max(2, count) :]


def _stock_symbols(cfg: V2Config) -> list[str]:
    _, rows = _published_rows(cfg, "stock_basic")
    symbols = sorted(
        {
            str(row.get("ts_code") or "").upper()
            for row in rows
            if str(row.get("list_status") or "L") == "L"
            and _CODE.fullmatch(str(row.get("ts_code") or "").upper())
        }
    )
    if not symbols:
        raise PublicEODError("verified stock_basic publication contains no supported symbols")
    return symbols


def _same_price(left: dict[str, object], right: dict[str, object]) -> bool:
    for field in ("open", "high", "low", "close"):
        if abs(float(str(left[field])) - float(str(right[field]))) > 1e-8:
            return False
    return True


def sync_public_eod(
    cfg: V2Config,
    *,
    target_date: str,
    provider_order: tuple[str, ...] = ("tencent", "sina"),
    lookback_sessions: int = 12,
    workers: int = 24,
    symbol_limit: int | None = None,
) -> dict[str, Any]:
    """Fetch and atomically publish a cumulative, display-only EOD supplement."""
    if any(item not in {"tencent", "sina"} for item in provider_order):
        raise ValueError("provider_order contains an unsupported provider")
    sessions = _open_sessions(cfg, target_date, lookback_sessions)
    allowed_dates = set(sessions)
    current, existing_rows = _published_rows(cfg, DATASET)
    symbols = _stock_symbols(cfg)
    if symbol_limit is not None:
        symbols = symbols[: max(1, symbol_limit)]
    if (
        str(current.get("end_date") or "") == target_date
        and symbol_limit is None
        and int(current.get("symbol_count") or 0) >= int(len(symbols) * 0.85)
    ):
        return {
            "status": "CURRENT",
            "dataset": DATASET,
            "target_date": target_date,
            "manifest_hash": current.get("manifest_hash"),
            "row_count": len(existing_rows),
            "symbol_count": len({str(row.get("ts_code")) for row in existing_rows}),
            "publication_updated": False,
        }

    datalen = max(20, lookback_sessions + 5)
    successes: dict[str, list[dict[str, object]]] = {}
    failures: dict[str, str] = {}
    providers: dict[str, int] = {name: 0 for name in provider_order}

    def fetch_one(ts_code: str) -> tuple[str, list[dict[str, object]], str]:
        errors: list[str] = []
        for provider in provider_order:
            try:
                rows = (
                    _fetch_tencent(ts_code, datalen, allowed_dates)
                    if provider == "tencent"
                    else _fetch_sina(ts_code, datalen, allowed_dates)
                )
                if rows:
                    return ts_code, rows, provider
                errors.append(f"{provider}:EMPTY_ALLOWED_WINDOW")
            except (OSError, ValueError, PublicEODError, requests.RequestException, json.JSONDecodeError) as exc:
                errors.append(f"{provider}:{type(exc).__name__}")
        raise PublicEODError(";".join(errors))

    # A complete pool must not issue thousands of per-security requests: that
    # trips public WAF/rate limits and cannot be called a reliable sync.  The
    # Tencent-first production path uses its batch quote endpoint for the
    # latest completed EOD. Diagnostic subsets still exercise the historical
    # K-line adapters above.
    if provider_order[0] == "tencent" and symbol_limit is None:
        batches = _chunks(symbols, 80)
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
            batch_futures = {
                executor.submit(_fetch_tencent_quote_batch, batch, target_date): batch
                for batch in batches
            }
            for future in as_completed(batch_futures):
                batch = batch_futures[future]
                try:
                    batch_values = future.result()
                    successes.update(batch_values)
                    providers["tencent"] += len(batch_values)
                    for batch_symbol in batch:
                        if batch_symbol not in batch_values:
                            failures[batch_symbol] = "MISSING_OR_NON_TARGET_QUOTE"
                except Exception as exc:
                    for batch_symbol in batch:
                        failures[batch_symbol] = type(exc).__name__
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 32))) as executor:
            symbol_futures = {
                executor.submit(fetch_one, symbol): symbol for symbol in symbols
            }
            for symbol_future in as_completed(symbol_futures):
                requested_symbol = symbol_futures[symbol_future]
                try:
                    fetched_code, fetched_rows, provider = symbol_future.result()
                    successes[fetched_code] = fetched_rows
                    providers[provider] = providers.get(provider, 0) + 1
                except Exception as exc:  # evidence stores type only, never bodies
                    failures[requested_symbol] = type(exc).__name__

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for row in existing_rows:
        date = str(row.get("trade_date") or "")
        code = str(row.get("ts_code") or "")
        if date <= target_date and _CODE.fullmatch(code):
            merged[(code, date)] = row
    conflicts: list[str] = []
    for successful_rows in successes.values():
        for row in successful_rows:
            key = (str(row["ts_code"]), str(row["trade_date"]))
            previous = merged.get(key)
            if previous is not None and not _same_price(previous, row):
                conflicts.append(f"{key[0]}:{key[1]}")
                continue
            if previous is None:
                merged[key] = row

    ordered = [merged[key] for key in sorted(merged)]
    latest_count = sum(1 for row in ordered if row.get("trade_date") == target_date)
    success_ratio = len(successes) / len(symbols)
    latest_ratio = latest_count / len(symbols)
    coverage_pass = success_ratio >= 0.85 and latest_ratio >= 0.80 and not conflicts
    result = sync_dataset(
        cfg,
        DATASET,
        preloaded_pages=[ordered],
        source_id=SOURCE_ID,
        request_override={
            "providers": list(provider_order),
            "provider_apis": [
                "tencent_completed_session_batch_quote",
                "tencent_unadjusted_day_diagnostic",
                "sina_kline_240_diagnostic",
            ],
            "target_date": target_date,
            "sessions": sessions,
            "requested_symbols": len(symbols),
            "successful_symbols": len(successes),
            "failed_symbols": len(failures),
            "latest_session_rows": latest_count,
            "price_basis": "UNADJUSTED_PRICE_ONLY",
            "unknown_fields_preserved_as_null": [
                "amount", "adj_factor", "up_limit", "down_limit", "is_suspended", "is_st", "board"
            ],
        },
        spec_override=SPEC,
        publish=coverage_pass and symbol_limit is None,
    )
    final_status = str(result.get("status")) if coverage_pass else "BLOCKED_DATA"
    report = {
        "schema_version": 1,
        "status": final_status,
        "dataset": DATASET,
        "target_date": target_date,
        "sessions": sessions,
        "price_basis": "UNADJUSTED_PRICE_ONLY",
        "research_authorized": False,
        "execution_eligible": False,
        "requested_symbols": len(symbols),
        "successful_symbols": len(successes),
        "failed_symbols": len(failures),
        "success_ratio": success_ratio,
        "latest_session_rows": latest_count,
        "latest_ratio": latest_ratio,
        "provider_successes": providers,
        "conflict_count": len(conflicts),
        "conflict_sample": conflicts[:20],
        "failure_types": {
            kind: sum(1 for value in failures.values() if value == kind)
            for kind in sorted(set(failures.values()))
        },
        "publication_updated": bool(result.get("publication_updated") and coverage_pass),
        "manifest_hash": result.get("manifest_hash"),
        "row_count": len(ordered),
        "unknown_fields_are_null": True,
        "qfq_fallback_used": False,
        "legacy_database_mutated": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    target = cfg.paths.artifacts / "evidence" / "MARKET_DATE_REPAIR" / "public_eod_sync.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, target)
    return report


__all__ = [
    "DATASET",
    "PublicEODError",
    "normalize_sina_payload",
    "normalize_tencent_quote_payload",
    "normalize_tencent_payload",
    "sync_public_eod",
]
