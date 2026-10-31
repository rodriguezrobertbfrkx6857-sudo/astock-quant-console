"""Read-only access to the published local Legacy evidence archive.

This module is part of the active V2 read path.  It never opens an external
project: the published pointer and its version directory must resolve inside
the main project.  Every response remains ``LEGACY_EVIDENCE`` and is kept
separate from V2 canonical truth, qualification and PAPER execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote

from src.quantcore.contracts.safety import PROJECT_ROOT, PathBoundary


LEGACY_PROVENANCE = "LEGACY_EVIDENCE"
LEGACY_RESULT = "LEGACY_RESULT"
LEGACY_EVENT = "LEGACY_EVENT"
LEGACY_PAPER = "LEGACY_READONLY_PAPER"
LOCAL_SOURCE_ID = "legacy-archive.integrated-v2"


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _json(value: object, default: object = None) -> object:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return _json_safe(value)
    try:
        return _json_safe(json.loads(str(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _text(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def _date(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("-", "")[:8]
    return text if len(text) == 8 and text.isdigit() else None


def _code(value: object) -> str:
    return _text(value).strip().upper()


def _hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_readonly_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_file() and sidecar.stat().st_size > 0:
            raise RuntimeError(f"local archive has an uncheckpointed sidecar: {sidecar.name}")
    encoded = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro&immutable=1",
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeError("local archive query_only could not be enabled")
    return connection


class LocalArchiveAdapter:
    """Query the atomically published local Legacy evidence version."""

    def __init__(self, published_root: str | Path):
        boundary = PathBoundary()
        published = boundary.require_readable(published_root)
        if not published.is_relative_to(PROJECT_ROOT):
            raise RuntimeError("local archive must be inside the main project")
        pointer_path = published / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if pointer.get("status") != "PUBLISHED":
            raise RuntimeError("local archive publication is not PUBLISHED")
        version = (published / str(pointer["version_path"])).resolve(strict=True)
        if not version.is_relative_to(PROJECT_ROOT):
            raise RuntimeError("local archive version escapes the main project")
        self.published_root = published
        self.root = version
        self.market_path = self.root / "market.db"
        self.lab_path = self.root / "lab.db"
        self.raw_path = self.root / "raw.db"
        self.pointer = pointer

    @contextmanager
    def _db(self, name: str) -> Iterator[sqlite3.Connection]:
        if name not in {"market.db", "lab.db"}:
            raise ValueError(f"unsupported local archive database: {name}")
        connection = _safe_readonly_connect(self.root / name)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _has(connection: sqlite3.Connection, table: str) -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        )

    @staticmethod
    def _rows(
        connection: sqlite3.Connection,
        sql: str,
        params: Sequence[object] = (),
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def _one(
        connection: sqlite3.Connection,
        sql: str,
        params: Sequence[object] = (),
    ) -> dict[str, Any] | None:
        row = connection.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _source(self, database: str, table: str, *, row_hash: str | None = None) -> dict[str, Any]:
        path = self.root / database
        stat = path.stat()
        snapshot = _hash({"path": database, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        return {
            "source_id": LOCAL_SOURCE_ID,
            "database": database,
            "table": table,
            "path_scope": "LOCAL_ARCHIVE_ONLY",
            "source_kind": LEGACY_PROVENANCE,
            "publication_state": LEGACY_PROVENANCE,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "snapshot_fingerprint": snapshot,
            "content_sha256": None,
            "row_hash": row_hash,
            "extracted_at": datetime.now(UTC).isoformat(),
        }

    def publication(
        self,
        dataset: str,
        database: str,
        table: str,
        *,
        row_count: int,
        symbol_count: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        quality: str = "UNKNOWN",
    ) -> dict[str, Any]:
        body = {
            "dataset": dataset,
            "database": database,
            "table": table,
            "row_count": row_count,
            "symbol_count": symbol_count,
            "start_date": start_date,
            "end_date": end_date,
            "quality": quality,
        }
        publication_hash = _hash(body)
        return {
            "publication_id": f"archive-{dataset}-{publication_hash[:16]}",
            "manifest_hash": publication_hash,
            "manifest_id": f"archive-manifest-{dataset}",
            "dataset": dataset,
            "provenance": LEGACY_PROVENANCE,
            "readiness": LEGACY_PROVENANCE,
            "quality_status": quality,
            "source": self._source(database, table, row_hash=publication_hash),
            "coverage": {
                "start": start_date,
                "end": end_date,
                "rows": row_count,
                "symbols": symbol_count,
            },
            "canonical_v2": False,
            "promotion": "FORBIDDEN_WITHOUT_V2_REVALIDATION",
        }

    def _count_coverage(
        self,
        connection: sqlite3.Connection,
        table: str,
        *,
        code_column: str = "ts_code",
        date_column: str = "trade_date",
    ) -> dict[str, Any]:
        if not self._has(connection, table):
            return {"rows": 0, "symbols": 0, "start": None, "end": None}
        try:
            row = self._one(
                connection,
                f"SELECT count(*) rows,count(DISTINCT {code_column}) symbols,min({date_column}) start,max({date_column}) end FROM {table}",
            )
        except sqlite3.DatabaseError:
            return {"rows": 0, "symbols": 0, "start": None, "end": None}
        row = row or {}
        return {
            "rows": int(row.get("rows") or 0),
            "symbols": int(row.get("symbols") or 0),
            "start": _date(row.get("start")),
            "end": _date(row.get("end")),
        }

    def catalog(self) -> dict[str, Any]:
        market_tables = ("bars", "etf_daily", "bench_daily", "sec_master", "norm_fund_basic", "data_health")
        lab_tables = (
            "strategy_registry", "factor_registry", "backtests", "bt_nav", "bt_trades", "bt_orders",
            "evaluations", "research_queue", "paper_account", "paper_positions", "paper_signals",
            "paper_orders", "paper_fills", "paper_nav", "event_store", "event_stats", "oos_registry",
        )
        datasets: list[dict[str, Any]] = []
        with self._db("market.db") as connection:
            present = self._tables(connection)
            for table in market_tables:
                if table not in present:
                    continue
                if table == "data_health":
                    for item in self._rows(connection, "SELECT dataset,rows_total,min_date,max_date,quality_score,status FROM data_health ORDER BY dataset"):
                        datasets.append({
                            "dataset": f"legacy.{item['dataset']}", "table": table, "database": "market.db",
                            "rows": int(item.get("rows_total") or 0), "symbols": None,
                            "start": _date(item.get("min_date")), "end": _date(item.get("max_date")),
                            "quality_score": item.get("quality_score"), "status": item.get("status"),
                            "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE,
                        })
                    continue
                coverage = self._count_coverage(connection, table, date_column="list_date" if table == "sec_master" else "trade_date")
                datasets.append({
                    "dataset": f"legacy.{table}", "table": table, "database": "market.db",
                    "rows": coverage["rows"], "symbols": coverage["symbols"], "start": coverage["start"], "end": coverage["end"],
                    "quality_score": None, "status": LEGACY_PROVENANCE,
                    "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE,
                })
            market_schema = sorted(present)
        with self._db("lab.db") as connection:
            present = self._tables(connection)
            for table in lab_tables:
                if table not in present:
                    continue
                if table in {"strategy_registry", "paper_account", "factor_registry", "event_stats"}:
                    code = "strategy_id" if table in {"strategy_registry", "paper_account"} else "factor_id" if table == "factor_registry" else "kind"
                    date_column = "created_at" if table in {"strategy_registry", "factor_registry"} else "updated_at" if table == "paper_account" else "computed_at"
                    coverage = self._count_coverage(connection, table, code_column=code, date_column=date_column)
                elif table in {"bt_nav", "paper_nav"}:
                    coverage = self._count_coverage(connection, table)
                elif table == "event_store":
                    coverage = self._count_coverage(connection, table, date_column="event_date")
                else:
                    coverage = self._count_coverage(connection, table, code_column="bt_id" if table in {"backtests", "bt_trades", "bt_orders"} else "strategy_id", date_column="created_at" if table == "backtests" else "trade_date")
                datasets.append({
                    "dataset": f"legacy.{table}", "table": table, "database": "lab.db",
                    "rows": coverage["rows"], "symbols": coverage["symbols"], "start": coverage["start"], "end": coverage["end"],
                    "quality_score": None, "status": LEGACY_PROVENANCE,
                    "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE,
                })
            lab_schema = sorted(present)
        return {
            "source_id": LOCAL_SOURCE_ID,
            "root_scope": "LOCAL_ARCHIVE_ONLY",
            "databases": [
                {"database": "market.db", "path_scope": "LOCAL_ARCHIVE_ONLY", "size_bytes": self.market_path.stat().st_size},
                {"database": "lab.db", "path_scope": "LOCAL_ARCHIVE_ONLY", "size_bytes": self.lab_path.stat().st_size},
            ],
            "datasets": datasets,
            "market_schema": market_schema,
            "lab_schema": lab_schema,
            "copy_policy": "BOUNDED_LOCAL_ARCHIVE_READONLY",
            "canonical_policy": "LEGACY_EVIDENCE_NEVER_AUTO_PROMOTED",
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def catalog_dataset_count(self) -> int:
        catalog_ref = self.pointer.get("catalog")
        if catalog_ref:
            try:
                path = (self.published_root / str(catalog_ref)).resolve(strict=True)
                if path.is_relative_to(PROJECT_ROOT):
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
                    if isinstance(datasets, list):
                        return len(datasets)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                pass
        return len(self.catalog().get("datasets", []))

    def security_search(self, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        needle = str(query or "").strip()
        like = f"%{needle}%"
        limit = max(1, min(int(limit), 1000))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self._db("market.db") as connection:
            if self._has(connection, "sec_master"):
                for row in self._rows(connection, "SELECT ts_code,symbol,name,exchange,board,sec_type,list_date,delist_date,list_status FROM sec_master WHERE (?='' OR ts_code LIKE ? OR symbol LIKE ? OR name LIKE ?) ORDER BY ts_code LIMIT ?", (needle, like, like, like, limit)):
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append({"ts_code": code, "symbol": _text(row.get("symbol")), "name": _text(row.get("name")) or code, "exchange": _text(row.get("exchange")), "board": _text(row.get("board")) or "UNKNOWN", "security_type": _text(row.get("sec_type")) or "UNKNOWN", "list_date": _date(row.get("list_date")), "delist_date": _date(row.get("delist_date")), "list_status": _text(row.get("list_status")), "source_table": "sec_master", "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE})
            if len(result) < limit and self._has(connection, "norm_fund_basic"):
                for row in self._rows(connection, "SELECT ts_code,symbol,name,market,list_date,delist_date,status FROM norm_fund_basic WHERE (?='' OR ts_code LIKE ? OR name LIKE ?) ORDER BY ts_code LIMIT ?", (needle, like, like, limit)):
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append({"ts_code": code, "symbol": _text(row.get("symbol")), "name": _text(row.get("name")) or code, "exchange": code.rsplit(".", 1)[-1], "board": "ETF", "security_type": "FUND", "list_date": _date(row.get("list_date")), "delist_date": _date(row.get("delist_date")), "list_status": _text(row.get("status")), "source_table": "norm_fund_basic", "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE})
            if len(result) < limit and self._has(connection, "bench_daily"):
                for row in self._rows(connection, "SELECT ts_code FROM (SELECT DISTINCT ts_code FROM bench_daily) WHERE (?='' OR ts_code LIKE ?) ORDER BY ts_code LIMIT ?", (needle, like, limit)):
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append({"ts_code": code, "symbol": code, "name": code, "exchange": code.rsplit(".", 1)[-1], "board": "INDEX", "security_type": "INDEX", "list_date": None, "delist_date": None, "list_status": "L", "source_table": "bench_daily", "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE})
        return result[:limit]

    def _market_table_for_code(self, connection: sqlite3.Connection, code: str) -> str | None:
        for table in ("etf_daily", "bars", "bench_daily"):
            if self._has(connection, table) and connection.execute(f"SELECT 1 FROM {table} WHERE ts_code=? LIMIT 1", (code,)).fetchone():
                return table
        return None

    def _count_code(self, connection: sqlite3.Connection, table: str | None, code: str) -> dict[str, Any]:
        if not table:
            return {"rows": 0, "start": None, "end": None}
        date_column = "list_date" if table == "sec_master" else "trade_date"
        predicate = (
            " AND (limit_source IS NULL OR lower(limit_source) NOT IN ('sina','tencent'))"
            if table == "bars"
            else ""
        )
        row = connection.execute(
            f"SELECT count(*) rows,min({date_column}) start,max({date_column}) end "
            f"FROM {table} WHERE ts_code=?{predicate}",
            (code,),
        ).fetchone()
        return {"rows": int(row[0] or 0), "start": _date(row[1]), "end": _date(row[2])}

    def security_detail(self, ts_code: str) -> dict[str, Any] | None:
        code = _code(ts_code)
        item = next((x for x in self.security_search(code, 10) if x["ts_code"] == code), None)
        with self._db("market.db") as connection:
            table = self._market_table_for_code(connection, code)
            coverage = self._count_code(connection, table, code)
        if item is None and table is None:
            return None
        item = item or {"ts_code": code, "symbol": code, "name": code, "exchange": code.rsplit(".", 1)[-1], "board": "UNKNOWN", "security_type": "UNKNOWN", "list_date": None, "delist_date": None, "list_status": "UNKNOWN", "source_table": table}
        item = dict(item)
        item.update({"coverage": coverage, "detail_source": self._source("market.db", str(item.get("source_table") or table or "unknown")), "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE})
        return item

    def daily(self, ts_code: str, start_date: str = "", end_date: str = "", limit: int = 5000) -> dict[str, Any]:
        code = _code(ts_code)
        start, end = _date(start_date), _date(end_date)
        limit = max(1, min(int(limit), 10000))
        with self._db("market.db") as connection:
            table = self._market_table_for_code(connection, code)
            if table is None:
                return {"ts_code": code, "rows": [], "count": 0, "source_table": None, "state": "NO_LEGACY_MARKET_DATA", "provenance": LEGACY_PROVENANCE}
            where = ["ts_code=?"]
            params: list[object] = [code]
            if start:
                where.append("trade_date>=?")
                params.append(start)
            if end:
                where.append("trade_date<=?")
                params.append(end)
            # A superseded fallback command wrote Sina/Tencent rows directly
            # into this immutable evidence archive and filled unknown fields
            # with zero/one. Preserve those rows physically for audit, but
            # never expose them through the trusted Legacy read path.
            if table == "bars":
                where.append("(limit_source IS NULL OR lower(limit_source) NOT IN ('sina','tencent'))")
            columns = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount,adj_factor,up_limit,down_limit,limit_source,is_suspended,is_st,board" if table == "bars" else "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
            rows = self._rows(connection, f"SELECT {columns} FROM {table} WHERE {' AND '.join(where)} ORDER BY trade_date DESC LIMIT ?", (*params, limit))
            rows.reverse()
        for row in rows:
            row.update({"ts_code": _code(row.get("ts_code")), "trade_date": _date(row.get("trade_date")), "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE, "source_table": table, "realtime_semantics": False})
        dates = [str(row["trade_date"]) for row in rows if row.get("trade_date")]
        row_hash = _hash(rows)
        publication = self.publication(f"legacy.{table}.{code}", "market.db", table, row_count=len(rows), symbol_count=1 if rows else 0, start_date=dates[0] if dates else start, end_date=dates[-1] if dates else end, quality="LEGACY_UNVERIFIED")
        return {"ts_code": code, "rows": rows, "data": rows, "count": len(rows), "available_dates": dates, "date_range": {"start": dates[0], "end": dates[-1]} if dates else None, "source_table": table, "source": self._source("market.db", table, row_hash=row_hash), "publication": publication, "manifest_hash": publication["manifest_hash"], "state": "LEGACY_READONLY_HISTORICAL" if rows else "NO_LEGACY_MARKET_DATA", "provenance": LEGACY_PROVENANCE, "realtime_semantics": False, "price_basis": "LEGACY_REPORTED_PRICE_UNVERIFIED_FOR_V2_EXECUTION"}

    def benchmarks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db("market.db") as connection:
            rows = self._rows(connection, "SELECT ts_code,count(*) rows,min(trade_date) start,max(trade_date) end FROM bench_daily GROUP BY ts_code ORDER BY ts_code LIMIT ?", (max(1, min(int(limit), 500)),))
        return [{"ts_code": _code(row["ts_code"]), "rows": int(row["rows"] or 0), "start": _date(row["start"]), "end": _date(row["end"]), "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE} for row in rows]

    @staticmethod
    def _normalize_evaluation(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["metrics"] = _json(item.get("metrics_json"), {})
        item["gates"] = _json(item.get("gates_json"), {})
        item["grade_reasons"] = _json(item.get("grade_reasons_json"), [])
        item["integrity"] = _json(item.get("integrity_json"), {})
        item["segments"] = _json(item.get("segments_json"), {})
        item["legacy_result"] = True
        return item

    def strategies(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT * FROM strategy_registry ORDER BY strategy_id LIMIT ?", (max(1, min(int(limit), 500)),))
            result: list[dict[str, Any]] = []
            for row in rows:
                sid = _text(row.get("strategy_id"))
                evaluation = self._one(connection, "SELECT * FROM evaluations WHERE strategy_id=? ORDER BY created_at DESC LIMIT 1", (sid,))
                backtest_count = int(connection.execute("SELECT count(*) FROM backtests WHERE strategy_id=?", (sid,)).fetchone()[0]) if self._has(connection, "backtests") else 0
                item = dict(row)
                item.update({"params": _json(item.get("params_json"), {}), "factors": _json(item.get("factors_json"), []), "legacy_status": "LEGACY_IMPORTED", "research_status": "RESEARCH_ONLY", "paper_eligible": False, "block_reasons": ["LEGACY_RESULT_REQUIRES_V2_REVALIDATION", "LEGACY_DATA_IS_NOT_V2_PAPER_QUALIFICATION"], "evaluation": self._normalize_evaluation(evaluation), "backtest_count": backtest_count, "provenance": LEGACY_PROVENANCE, "source": self._source("lab.db", "strategy_registry", row_hash=_hash(item)), "publication": self.publication("legacy.strategy_registry", "lab.db", "strategy_registry", row_count=len(rows), symbol_count=len(rows), quality="LEGACY_UNVERIFIED")})
                result.append(item)
        return result

    def _backtests_for_strategy(self, connection: sqlite3.Connection, strategy_id: str | None, limit: int) -> list[dict[str, Any]]:
        params: list[object] = []
        where = ""
        if strategy_id:
            where = "WHERE b.strategy_id=?"
            params.append(strategy_id)
        params.append(max(1, min(int(limit), 500)))
        rows = self._rows(connection, "SELECT b.*,count(n.trade_date) n_days,min(n.trade_date) nav_start,max(n.trade_date) nav_end,min(n.nav) first_nav FROM backtests b LEFT JOIN bt_nav n ON n.bt_id=b.bt_id " + where + " GROUP BY b.bt_id ORDER BY b.created_at DESC LIMIT ?", params)
        out: list[dict[str, Any]] = []
        for row in rows:
            bid = _text(row.get("bt_id"))
            first = self._one(connection, "SELECT nav,bench_nav FROM bt_nav WHERE bt_id=? ORDER BY trade_date LIMIT 1", (bid,))
            last = self._one(connection, "SELECT nav,bench_nav FROM bt_nav WHERE bt_id=? ORDER BY trade_date DESC LIMIT 1", (bid,))
            item = dict(row)
            first_nav, last_nav = (float(first["nav"]) if first and first.get("nav") is not None else None), (float(last["nav"]) if last and last.get("nav") is not None else None)
            first_bench, last_bench = (float(first["bench_nav"]) if first and first.get("bench_nav") is not None else None), (float(last["bench_nav"]) if last and last.get("bench_nav") is not None else None)
            item.update({"params": _json(item.get("params_json"), {}), "strategy_name": self._strategy_name(connection, _text(row.get("strategy_id"))), "total_return": last_nav / first_nav - 1 if first_nav and last_nav is not None else None, "bench_return": last_bench / first_bench - 1 if first_bench and last_bench is not None else None, "legacy_status": LEGACY_RESULT, "v2_revalidated": False, "paper_eligible": False, "provenance": LEGACY_PROVENANCE, "source": self._source("lab.db", "backtests", row_hash=_hash(item))})
            out.append(item)
        return out

    @staticmethod
    def _strategy_name(connection: sqlite3.Connection, strategy_id: str) -> str:
        row = connection.execute("SELECT name FROM strategy_registry WHERE strategy_id=?", (strategy_id,)).fetchone()
        return _text(row[0]) if row else strategy_id

    def backtests(self, strategy_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            return self._backtests_for_strategy(connection, strategy_id, limit)

    def strategy_detail(self, strategy_id: str) -> dict[str, Any] | None:
        item = next((x for x in self.strategies(500) if x["strategy_id"] == strategy_id), None)
        if item is None:
            return None
        with self._db("lab.db") as connection:
            item["evaluations"] = [self._normalize_evaluation(row) for row in self._rows(connection, "SELECT * FROM evaluations WHERE strategy_id=? ORDER BY created_at", (strategy_id,))]
            item["backtests"] = self._backtests_for_strategy(connection, strategy_id, 200)
            item["research_queue"] = self._rows(connection, "SELECT * FROM research_queue WHERE target=? OR strategy_id=? ORDER BY priority,created_at LIMIT 100", (strategy_id, strategy_id))
        item["detail_state"] = "LEGACY_READONLY_DETAIL"
        return item

    def factors(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            registry = self._rows(connection, "SELECT * FROM factor_registry ORDER BY factor_id LIMIT ?", (max(1, min(int(limit), 500)),))
            result: list[dict[str, Any]] = []
            for row in registry:
                fid = _text(row.get("factor_id"))
                stats = self._one(connection, "SELECT * FROM factor_stats WHERE factor_id=? ORDER BY CASE WHEN scope='research' THEN 0 ELSE 1 END LIMIT 1", (fid,))
                queue_row = self._one(connection, "SELECT * FROM research_queue WHERE target=? ORDER BY created_at DESC LIMIT 1", (fid,))
                item = dict(row)
                params = _json(item.get("params_json"), {})
                direction = params.get("direction") if isinstance(params, dict) else None
                item.update({"params": params, "stats": dict(stats) if stats else {}, "result": _json(queue_row.get("result_json"), {}) if queue_row else {}, "direction": _text(direction, "UNKNOWN"), "legacy_status": "LEGACY_IMPORTED", "research_status": "RESEARCH_ONLY", "accepted": False, "paper_eligible": False, "block_reasons": ["LEGACY_FACTOR_RESULT", "V2_FACTOR_REVALIDATION_REQUIRED"], "provenance": LEGACY_PROVENANCE, "source": self._source("lab.db", "factor_registry", row_hash=_hash(item))})
                result.append(item)
        return result

    def factor_detail(self, factor_id: str, limit: int = 250) -> dict[str, Any] | None:
        item = next((x for x in self.factors(500) if x["factor_id"] == factor_id), None)
        if item is None:
            return None
        with self._db("lab.db") as connection:
            stats = self._rows(connection, "SELECT * FROM factor_stats WHERE factor_id=? ORDER BY scope", (factor_id,))
            ic = self._rows(connection, "SELECT * FROM factor_ic_daily WHERE factor_id=? ORDER BY trade_date DESC LIMIT ?", (factor_id, max(1, min(int(limit), 1000))))
            coverage = self._one(connection, "SELECT row_count rows,symbol_count symbols,start_date start,end_date end FROM factor_coverage WHERE factor_id=?", (factor_id,))
        coverage = coverage or {}
        item.update({"stats_history": stats, "ic_daily": ic, "coverage": {"rows": int(coverage.get("rows") or 0), "symbols": int(coverage.get("symbols") or 0), "start": _date(coverage.get("start")), "end": _date(coverage.get("end"))}, "detail_state": "LEGACY_READONLY_DETAIL", "research_evidence": item.get("source")})
        return item

    def backtest_detail(self, backtest_id: str) -> dict[str, Any] | None:
        with self._db("lab.db") as connection:
            item = next((x for x in self._backtests_for_strategy(connection, None, 500) if x["bt_id"] == backtest_id), None)
            if item is None:
                return None
            item.update({"trade_count": int(connection.execute("SELECT count(*) FROM bt_trades WHERE bt_id=?", (backtest_id,)).fetchone()[0]), "order_count": int(connection.execute("SELECT count(*) FROM bt_orders WHERE bt_id=?", (backtest_id,)).fetchone()[0]), "validation": self._rows(connection, "SELECT * FROM validation_runs WHERE bt_id=? ORDER BY created_at DESC", (backtest_id,)), "metrics": self._backtest_metrics(connection, backtest_id), "detail_state": "LEGACY_READONLY_DETAIL"})
            return item

    def _backtest_metrics(self, connection: sqlite3.Connection, backtest_id: str) -> dict[str, Any]:
        rows = self._rows(connection, "SELECT trade_date,nav,ret,bench_nav,bench_ret,exposure FROM bt_nav WHERE bt_id=? ORDER BY trade_date", (backtest_id,))
        navs = [float(row["nav"]) for row in rows if row.get("nav") is not None]
        if not navs:
            return {"n_days": 0}
        rets = [float(row["ret"]) for row in rows if row.get("ret") is not None]
        peak, max_dd = navs[0], 0.0
        for value in navs:
            peak = max(peak, value)
            max_dd = max(max_dd, 1 - value / peak)
        mean = sum(rets) / len(rets) if rets else 0.0
        variance = sum((value - mean) ** 2 for value in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
        std = variance**0.5
        exposures = [float(row["exposure"]) for row in rows if row.get("exposure") is not None]
        return {"n_days": len(navs), "total_return": navs[-1] / navs[0] - 1 if navs[0] else None, "max_drawdown": max_dd, "sharpe_annualized_244": mean / std * (244**0.5) if std else None, "average_exposure": sum(exposures) / len(exposures) if exposures else None, "legacy_metric_convention": "LEGACY_RESULT; NOT V2_CANONICAL"}

    def backtest_nav(self, backtest_id: str, limit: int = 5000) -> dict[str, Any] | None:
        detail = self.backtest_detail(backtest_id)
        if detail is None:
            return None
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT trade_date,nav,ret,bench_nav,bench_ret,exposure FROM bt_nav WHERE bt_id=? ORDER BY trade_date LIMIT ?", (backtest_id, max(1, min(int(limit), 10000))))
        navs = [float(row["nav"]) for row in rows if row.get("nav") is not None]
        bench = [float(row["bench_nav"]) for row in rows if row.get("bench_nav") is not None]
        nav0, bench0, peak = (navs[0] if navs else None), (bench[0] if bench else None), (navs[0] if navs else 0.0)
        series = []
        for row in rows:
            nav = float(row["nav"]) if row.get("nav") is not None else None
            if nav is not None:
                peak = max(peak, nav)
            series.append({**row, "nav_normalized": nav / nav0 if nav is not None and nav0 else None, "bench_normalized": float(row["bench_nav"]) / bench0 if row.get("bench_nav") is not None and bench0 else None, "drawdown": 1 - nav / peak if nav is not None and peak else None, "provenance": LEGACY_PROVENANCE})
        return {"backtest_id": backtest_id, "bt_id": backtest_id, "strategy_id": detail["strategy_id"], "strategy_name": detail["strategy_name"], "series": series, "rows": series, "summary": detail["metrics"], "legacy_status": LEGACY_RESULT, "publication": self.publication(f"legacy.backtest.{backtest_id}.nav", "lab.db", "bt_nav", row_count=len(series), symbol_count=1, start_date=series[0].get("trade_date") if series else None, end_date=series[-1].get("trade_date") if series else None, quality="LEGACY_UNVERIFIED"), "source": self._source("lab.db", "bt_nav", row_hash=_hash(series))}

    def backtest_trades(self, backtest_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT * FROM bt_trades WHERE bt_id=? ORDER BY trade_date,seq LIMIT ?", (backtest_id, max(1, min(int(limit), 10000))))
        for row in rows:
            row.update({"trade_id": f"{backtest_id}:{row.get('seq')}", "provenance": LEGACY_PROVENANCE, "legacy_status": LEGACY_RESULT, "source_table": "bt_trades"})
        return rows

    def backtest_fills(self, backtest_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        return self.backtest_trades(backtest_id, limit)

    def backtest_orders(self, backtest_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT * FROM bt_orders WHERE bt_id=? ORDER BY issued_date,seq LIMIT ?", (backtest_id, max(1, min(int(limit), 10000))))
        for row in rows:
            row.update({"order_id": f"{backtest_id}:{row.get('seq')}", "provenance": LEGACY_PROVENANCE, "legacy_status": LEGACY_RESULT, "source_table": "bt_orders"})
        return rows

    def research_queue(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT * FROM research_queue ORDER BY status,priority,created_at,item_id LIMIT ?", (max(1, min(int(limit), 500)),))
        result = []
        for row in rows:
            state = _text(row.get("status") or row.get("state"), "UNKNOWN")
            row.update({"status": state, "legacy_state": state, "missing_dependencies": ["LEGACY_DATA_NOT_V2_CANONICAL"], "dataset": "legacy.quant3", "oos_status": "LEGACY_EVIDENCE_ONLY", "current_step": "READONLY_IMPORT", "runtime_job": None, "last_error": row.get("last_error") or row.get("reason"), "run_allowed": False, "block_reasons": ["LEGACY_QUEUE_CANNOT_EXECUTE_DIRECTLY", "V2_RUN_REQUIRES_CURRENT_GATE"], "provenance": LEGACY_PROVENANCE})
            result.append(row)
        return result

    @staticmethod
    def _decode_json_field(row: dict[str, Any], field: str) -> dict[str, Any]:
        value = dict(row)
        value[field.removesuffix("_json")] = _json(value.get(field), value.get(field))
        return value

    def paper_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db("lab.db") as connection:
            rows = self._rows(connection, "SELECT * FROM paper_account ORDER BY strategy_id LIMIT ?", (max(1, min(int(limit), 200)),))
            result = []
            for row in rows:
                sid = _text(row.get("strategy_id"))
                positions = self._rows(connection, "SELECT * FROM paper_positions WHERE strategy_id=? AND shares>0 ORDER BY shares*coalesce(last_price,0) DESC", (sid,))
                nav = self._rows(connection, "SELECT * FROM paper_nav WHERE strategy_id=? ORDER BY trade_date", (sid,))
                signals = self._rows(connection, "SELECT * FROM paper_signals WHERE strategy_id=? ORDER BY signal_date DESC LIMIT 100", (sid,))
                orders = self._rows(connection, "SELECT * FROM paper_orders WHERE strategy_id=? ORDER BY order_date DESC LIMIT 100", (sid,))
                fills = self._rows(connection, "SELECT * FROM paper_fills WHERE strategy_id=? ORDER BY trade_date DESC LIMIT 100", (sid,))
                item = dict(row)
                item.update({"status": LEGACY_PAPER, "legacy_status": LEGACY_PAPER, "paper_eligible": False, "execution_allowed": False, "block_reasons": ["LEGACY_PAPER_READONLY", "CURRENT_V2_QUALIFICATION_REQUIRED"], "positions": positions, "nav_series": nav, "signals": [self._decode_json_field(x, "payload_json") for x in signals], "orders": orders, "fills": fills, "ledger": [], "reconciliation_status": "LEGACY_READONLY_NOT_RECONCILED_IN_V2", "provenance": LEGACY_PROVENANCE, "source": self._source("lab.db", "paper_account", row_hash=_hash(item))})
                result.append(item)
        return result

    def paper_detail(self, strategy_id: str) -> dict[str, Any] | None:
        return next((item for item in self.paper_accounts(200) if item["strategy_id"] == strategy_id), None)

    def events(self, limit: int = 100, kind: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._db("lab.db") as connection:
            params: list[object] = []
            where = "1=1"
            if kind:
                where = "e.kind=?"
                params.append(kind)
            rows = self._rows(connection, "SELECT e.event_id,e.ts_code,e.company_name,e.kind,e.score,e.event_ts,e.event_date,e.source,e.title,e.ref_id,e.created_at FROM event_store e WHERE " + where + " ORDER BY e.event_date DESC,e.event_id LIMIT ?", (*params, limit))
            stats = self._rows(connection, "SELECT * FROM event_stats WHERE scope='research' ORDER BY n_events DESC")
            total = int(connection.execute("SELECT count(*) FROM event_store").fetchone()[0])
        for row in rows:
            row.update({"event_type": row.get("kind"), "published_at": _date(row.get("event_date")), "summary": f"{row.get('company_name') or row.get('ts_code') or ''} · {row.get('kind') or ''}", "source_kind": LEGACY_EVENT, "provenance": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE, "evidence_ref": f"legacy-archive://event_store/{row.get('event_id')}"})
        return {"events": rows, "count": total, "kind_stats": stats, "state": "LEGACY_HISTORY_AVAILABLE", "live_vendor": "PENDING_VENDOR", "event_engine_ready": True, "provenance": LEGACY_PROVENANCE, "publication": self.publication("legacy.event_store", "lab.db", "event_store", row_count=total, quality="LEGACY_UNVERIFIED")}

    def data_health(self) -> dict[str, Any]:
        with self._db("market.db") as connection:
            rows = self._rows(connection, "SELECT * FROM data_health ORDER BY dataset")
        coverage = {}
        for row in rows:
            dataset = _text(row.get("dataset"))
            coverage[dataset] = {"dataset": dataset, "source": LOCAL_SOURCE_ID, "source_kind": LEGACY_PROVENANCE, "publication_state": LEGACY_PROVENANCE, "total_rows": int(row.get("rows_total") or 0), "row_count": int(row.get("rows_total") or 0), "earliest_date": _date(row.get("min_date")), "latest_date": _date(row.get("max_date")), "quality_score": row.get("quality_score"), "status": _text(row.get("status"), "UNKNOWN"), "stale_days": row.get("stale_days"), "detail": row.get("detail")}
        return {"overall_source": LOCAL_SOURCE_ID, "provenance_status": LEGACY_PROVENANCE, "coverage": coverage, "catalog": list(coverage.values()), "watermarks": [], "recent_ingestions": 0, "recent_errors": [], "message": "Historical data is served from the local immutable Legacy evidence archive."}

    def predict(self, ts_code: str, start_date: str = "", end_date: str = "") -> dict[str, Any]:
        daily = self.daily(ts_code, start_date, end_date, limit=500)
        rows = daily["rows"]
        closes = [float(row["close"]) for row in rows if row.get("close") is not None]
        def ret(n: int) -> float | None:
            return None if len(closes) <= n or closes[-n - 1] == 0 else closes[-1] / closes[-n - 1] - 1
        r5, r20 = ret(5), ret(20)
        recent = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]][-20:]
        mean = sum(recent) / len(recent) if recent else 0
        variance = sum((x - mean) ** 2 for x in recent) / (len(recent) - 1) if len(recent) > 1 else 0
        return {"ts_code": _code(ts_code), "last_date": rows[-1].get("trade_date") if rows else None, "last_close": closes[-1] if closes else None, "return_5d": r5, "return_20d": r20, "volatility_20d": variance**0.5 if variance else None, "historical_regime": "UPTREND" if r20 is not None and r20 > 0.05 else "DOWNTREND" if r20 is not None and r20 < -0.05 else "MIXED", "research_only": True, "experimental": True, "trading_signal": False, "paper_eligible": False, "label": "EXPERIMENTAL / RESEARCH_ONLY", "provenance": daily.get("provenance", LEGACY_PROVENANCE), "source": daily.get("source")}

    def readonly_check(self) -> dict[str, Any]:
        checks = []
        for database in ("market.db", "lab.db"):
            try:
                with self._db(database) as connection:
                    query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
                    checks.append({"database": database, "exists": True, "query_only": query_only, "read_only": query_only == 1, "mutation_attempted": False})
            except Exception as exc:
                checks.append({"database": database, "exists": False, "read_only": False, "mutation_attempted": False, "error": str(exc)})
        return {"status": "PASS" if checks and all(item.get("read_only") for item in checks) else "FAIL", "checks": checks, "legacy_write_attempted": False, "policy": "LOCAL_ARCHIVE_READ_ONLY", "generated_at": datetime.now(UTC).isoformat()}
