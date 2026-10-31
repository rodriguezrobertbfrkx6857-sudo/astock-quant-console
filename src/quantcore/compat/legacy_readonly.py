"""Lazy, read-only adapters for the historical qforge/AQuantLab data surface.

The product needs to show historical research assets without copying several
million market rows into the V2 control database. This adapter therefore:

* opens existing Legacy SQLite files with mode=ro&immutable=1;
* rejects databases with non-empty WAL/journal sidecars;
* exposes bounded, indexed queries and small metadata manifests;
* labels every returned object as LEGACY_EVIDENCE;
* never writes a Legacy file and never claims Legacy results are V2-validated.

The adapter is intentionally a data-access layer. It does not call Legacy
research or PAPER code, and it cannot change Stage09, Stage10, qualification,
canonical publication, or real/PAPER execution state.
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

from src.quantcore.contracts.safety import PathBoundary


LEGACY_SOURCE_ID = "legacy-readonly.quant3.data"
LEGACY_PROVENANCE = "LEGACY_EVIDENCE"
LEGACY_RESULT = "LEGACY_RESULT"
LEGACY_EVENT = "LEGACY_EVENT"
LEGACY_PAPER = "LEGACY_READONLY_PAPER"


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
    if value is None:
        return default
    return str(value)


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
            raise RuntimeError(
                f"legacy snapshot has an uncheckpointed sidecar: {sidecar.name}"
            )
    encoded = quote(path.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(
        f"file:{encoded}?mode=ro&immutable=1",
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class LegacyReadonlyAdapter:
    """Read-only access to the historical qforge data repository."""

    def __init__(self, root: str | Path):
        self.root = PathBoundary().require_legacy_readonly(root)
        self.market_path = self.root / "market.db"
        self.lab_path = self.root / "lab.db"

    @contextmanager
    def _db(self, name: str) -> Iterator[sqlite3.Connection]:
        if name not in {"market.db", "lab.db"}:
            raise ValueError(f"unsupported legacy database: {name}")
        conn = _safe_readonly_connect(self.root / name)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _has(conn: sqlite3.Connection, table: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        )

    @staticmethod
    def _rows(
        conn: sqlite3.Connection,
        sql: str,
        params: Sequence[object] = (),
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    @staticmethod
    def _one(
        conn: sqlite3.Connection,
        sql: str,
        params: Sequence[object] = (),
    ) -> dict[str, Any] | None:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _db_fingerprint(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        fingerprint = _hash(
            {"path": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        )
        return {
            "database": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "snapshot_fingerprint": fingerprint,
            "content_sha256": None,
            "content_hash_status": "NOT_COMPUTED_FOR_LARGE_READONLY_DB",
        }

    def _source(
        self, database: str, table: str, *, row_hash: str | None = None
    ) -> dict[str, Any]:
        path = self.root / database
        source = self._db_fingerprint(path)
        source.update(
            {
                "source_id": LEGACY_SOURCE_ID,
                "database": database,
                "table": table,
                "source_kind": LEGACY_PROVENANCE,
                "publication_state": "LEGACY_EVIDENCE",
                "row_hash": row_hash,
                "extracted_at": datetime.now(UTC).isoformat(),
            }
        )
        return source

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
            "publication_id": f"legacy-{dataset}-{publication_hash[:16]}",
            "manifest_hash": publication_hash,
            "manifest_id": f"legacy-manifest-{dataset}",
            "dataset": dataset,
            "provenance": LEGACY_PROVENANCE,
            "readiness": "LEGACY_EVIDENCE",
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
        conn: sqlite3.Connection,
        table: str,
        *,
        code_column: str = "ts_code",
        date_column: str = "trade_date",
    ) -> dict[str, Any]:
        if not self._has(conn, table):
            return {"rows": 0, "symbols": 0, "start": None, "end": None}
        try:
            row = self._one(
                conn,
                f"SELECT count(*) rows, count(DISTINCT {code_column}) symbols, "
                f"min({date_column}) start, max({date_column}) end FROM {table}",
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
        """Return a bounded metadata catalog without copying data rows."""
        market_tables = (
            "bars",
            "etf_daily",
            "bench_daily",
            "sec_master",
            "norm_daily",
            "norm_daily_basic",
            "norm_news",
            "data_health",
        )
        lab_tables = (
            "strategy_registry",
            "factor_registry",
            "backtests",
            "bt_nav",
            "bt_trades",
            "bt_orders",
            "evaluations",
            "research_queue",
            "paper_account",
            "paper_positions",
            "paper_signals",
            "paper_orders",
            "paper_fills",
            "paper_nav",
            "event_store",
            "event_stats",
            "oos_registry",
        )
        datasets: list[dict[str, Any]] = []
        with self._db("market.db") as conn:
            market_tables_present = self._tables(conn)
            for table in market_tables:
                if table not in market_tables_present:
                    continue
                if table == "sec_master":
                    coverage = self._count_coverage(
                        conn, table, code_column="ts_code", date_column="list_date"
                    )
                elif table == "data_health":
                    rows = self._rows(
                        conn,
                        "SELECT dataset,rows_total,min_date,max_date,quality_score,status "
                        "FROM data_health ORDER BY dataset",
                    )
                    for item in rows:
                        datasets.append(
                            {
                                "dataset": f"legacy.{item['dataset']}",
                                "table": "data_health",
                                "database": "market.db",
                                "rows": int(item.get("rows_total") or 0),
                                "symbols": None,
                                "start": _date(item.get("min_date")),
                                "end": _date(item.get("max_date")),
                                "quality_score": item.get("quality_score"),
                                "status": item.get("status"),
                                "provenance": LEGACY_PROVENANCE,
                                "publication_state": "LEGACY_EVIDENCE",
                            }
                        )
                    continue
                else:
                    coverage = self._count_coverage(conn, table)
                datasets.append(
                    {
                        "dataset": f"legacy.{table}",
                        "table": table,
                        "database": "market.db",
                        "rows": coverage["rows"],
                        "symbols": coverage["symbols"],
                        "start": coverage["start"],
                        "end": coverage["end"],
                        "quality_score": None,
                        "status": "LEGACY_EVIDENCE",
                        "provenance": LEGACY_PROVENANCE,
                        "publication_state": "LEGACY_EVIDENCE",
                    }
                )
            market_schema = sorted(market_tables_present)

        with self._db("lab.db") as conn:
            lab_tables_present = self._tables(conn)
            for table in lab_tables:
                if table not in lab_tables_present:
                    continue
                if table in {"strategy_registry", "factor_registry", "paper_account", "event_stats"}:
                    coverage = self._count_coverage(
                        conn,
                        table,
                        code_column=(
                            "strategy_id"
                            if table in {"strategy_registry", "paper_account"}
                            else "factor_id"
                            if table == "factor_registry"
                            else "kind"
                        ),
                        date_column=(
                            "created_at"
                            if table in {"strategy_registry", "factor_registry"}
                            else "updated_at"
                            if table == "paper_account"
                            else "computed_at"
                        ),
                    )
                elif table in {"bt_nav", "paper_nav"}:
                    coverage = self._count_coverage(conn, table)
                elif table == "event_store":
                    coverage = self._count_coverage(
                        conn, table, code_column="ts_code", date_column="event_date"
                    )
                else:
                    coverage = self._count_coverage(
                        conn,
                        table,
                        code_column=(
                            "bt_id"
                            if table in {"backtests", "bt_trades", "bt_orders"}
                            else "strategy_id"
                        ),
                        date_column="created_at"
                        if table in {"backtests"}
                        else "trade_date",
                    )
                datasets.append(
                    {
                        "dataset": f"legacy.{table}",
                        "table": table,
                        "database": "lab.db",
                        "rows": coverage["rows"],
                        "symbols": coverage["symbols"],
                        "start": coverage["start"],
                        "end": coverage["end"],
                        "quality_score": None,
                        "status": "LEGACY_EVIDENCE",
                        "provenance": LEGACY_PROVENANCE,
                        "publication_state": "LEGACY_EVIDENCE",
                    }
                )
            lab_schema = sorted(lab_tables_present)
        return {
            "source_id": LEGACY_SOURCE_ID,
            "root": str(self.root),
            "databases": [
                self._db_fingerprint(self.market_path),
                self._db_fingerprint(self.lab_path),
            ],
            "datasets": datasets,
            "market_schema": market_schema,
            "lab_schema": lab_schema,
            "copy_policy": "LAZY_READONLY_NO_MILLION_ROW_COPY",
            "canonical_policy": "LEGACY_EVIDENCE_NEVER_AUTO_PROMOTED",
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def catalog_dataset_count(self) -> int:
        """Count catalog entries without scanning any legacy data table.

        The full catalog intentionally computes coverage for its data-browser
        caller.  System health only needs the number of visible entries, so a
        full ``count(distinct ...)`` over multi-gigabyte legacy tables makes
        that health endpoint unusably slow.  This method inspects SQLite
        schemas and the already-materialized ``data_health`` summary only.
        """
        market_tables = {
            "bars",
            "etf_daily",
            "bench_daily",
            "sec_master",
            "norm_daily",
            "norm_daily_basic",
            "norm_news",
        }
        lab_tables = {
            "strategy_registry",
            "factor_registry",
            "backtests",
            "bt_nav",
            "bt_trades",
            "bt_orders",
            "evaluations",
            "research_queue",
            "paper_account",
            "paper_positions",
            "paper_signals",
            "paper_orders",
            "paper_fills",
            "paper_nav",
            "event_store",
            "event_stats",
            "oos_registry",
        }
        with self._db("market.db") as conn:
            present = self._tables(conn)
            count = len(market_tables & present)
            if "data_health" in present:
                count += int(conn.execute("SELECT count(*) FROM data_health").fetchone()[0])
        with self._db("lab.db") as conn:
            count += len(lab_tables & self._tables(conn))
        return count

    def security_search(self, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        needle = str(query or "").strip()
        like = f"%{needle}%"
        limit = max(1, min(int(limit), 1000))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self._db("market.db") as conn:
            if self._has(conn, "sec_master"):
                rows = self._rows(
                    conn,
                    "SELECT ts_code,symbol,name,exchange,board,sec_type,list_date,"
                    "delist_date,list_status FROM sec_master "
                    "WHERE (?='' OR ts_code LIKE ? OR symbol LIKE ? OR name LIKE ?) "
                    "ORDER BY ts_code LIMIT ?",
                    (needle, like, like, like, limit),
                )
                for row in rows:
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append(
                        {
                            "ts_code": code,
                            "symbol": _text(row.get("symbol")),
                            "name": _text(row.get("name")) or code,
                            "exchange": _text(row.get("exchange")),
                            "board": _text(row.get("board")) or "UNKNOWN",
                            "security_type": _text(row.get("sec_type")) or "UNKNOWN",
                            "list_date": _date(row.get("list_date")),
                            "delist_date": _date(row.get("delist_date")),
                            "list_status": _text(row.get("list_status")),
                            "source_table": "sec_master",
                            "provenance": LEGACY_PROVENANCE,
                            "publication_state": "LEGACY_EVIDENCE",
                        }
                    )
            if len(result) < limit and self._has(conn, "norm_fund_basic"):
                rows = self._rows(
                    conn,
                    "SELECT ts_code,symbol,name,market,list_date,delist_date,status "
                    "FROM norm_fund_basic WHERE (?='' OR ts_code LIKE ? OR name LIKE ?) "
                    "ORDER BY ts_code LIMIT ?",
                    (needle, like, like, limit),
                )
                for row in rows:
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append(
                        {
                            "ts_code": code,
                            "symbol": _text(row.get("symbol")),
                            "name": _text(row.get("name")) or code,
                            "exchange": code.rsplit(".", 1)[-1],
                            "board": "ETF",
                            "security_type": "FUND",
                            "list_date": _date(row.get("list_date")),
                            "delist_date": _date(row.get("delist_date")),
                            "list_status": _text(row.get("status")),
                            "source_table": "norm_fund_basic",
                            "provenance": LEGACY_PROVENANCE,
                            "publication_state": "LEGACY_EVIDENCE",
                        }
                    )
            if len(result) < limit and self._has(conn, "bench_daily"):
                rows = self._rows(
                    conn,
                    "SELECT ts_code FROM (SELECT DISTINCT ts_code FROM bench_daily) "
                    "WHERE (?='' OR ts_code LIKE ?) ORDER BY ts_code LIMIT ?",
                    (needle, like, limit),
                )
                for row in rows:
                    code = _code(row.get("ts_code"))
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    result.append(
                        {
                            "ts_code": code,
                            "symbol": code,
                            "name": code,
                            "exchange": code.rsplit(".", 1)[-1],
                            "board": "INDEX",
                            "security_type": "INDEX",
                            "list_date": None,
                            "delist_date": None,
                            "list_status": "L",
                            "source_table": "bench_daily",
                            "provenance": LEGACY_PROVENANCE,
                            "publication_state": "LEGACY_EVIDENCE",
                        }
                    )
        return result[:limit]

    def security_detail(self, ts_code: str) -> dict[str, Any] | None:
        code = _code(ts_code)
        matches = self.security_search(code, 10)
        item = next((x for x in matches if x["ts_code"] == code), None)
        with self._db("market.db") as conn:
            table = self._market_table_for_code(conn, code)
            coverage = (
                self._count_code(conn, table, code)
                if table is not None
                else {"rows": 0, "start": None, "end": None}
            )
            if item is None and table is None:
                return None
            if item is None:
                item = {
                    "ts_code": code,
                    "symbol": code,
                    "name": code,
                    "exchange": code.rsplit(".", 1)[-1],
                    "board": "UNKNOWN",
                    "security_type": "UNKNOWN",
                    "list_date": None,
                    "delist_date": None,
                    "list_status": "UNKNOWN",
                    "source_table": table,
                }
        item = dict(item)
        item.update(
            {
                "coverage": coverage,
                "detail_source": self._source(
                    "market.db", str(item.get("source_table") or table or "unknown")
                ),
                "provenance": LEGACY_PROVENANCE,
                "publication_state": "LEGACY_EVIDENCE",
            }
        )
        return item

    @staticmethod
    def _count_code(
        conn: sqlite3.Connection, table: str | None, code: str
    ) -> dict[str, Any]:
        if not table:
            return {"rows": 0, "start": None, "end": None}
        date_column = "list_date" if table == "sec_master" else "trade_date"
        try:
            row = conn.execute(
                f"SELECT count(*) rows,min({date_column}) start,max({date_column}) end "
                f"FROM {table} WHERE ts_code=?",
                (code,),
            ).fetchone()
        except sqlite3.DatabaseError:
            return {"rows": 0, "start": None, "end": None}
        return {
            "rows": int(row[0] or 0),
            "start": _date(row[1]),
            "end": _date(row[2]),
        }

    def _market_table_for_code(
        self, conn: sqlite3.Connection, code: str
    ) -> str | None:
        for table in ("etf_daily", "bars", "bench_daily"):
            if self._has(conn, table) and conn.execute(
                f"SELECT 1 FROM {table} WHERE ts_code=? LIMIT 1", (code,)
            ).fetchone():
                return table
        return None

    def daily(
        self,
        ts_code: str,
        start_date: str = "",
        end_date: str = "",
        limit: int = 5000,
    ) -> dict[str, Any]:
        code = _code(ts_code)
        start = _date(start_date)
        end = _date(end_date)
        limit = max(1, min(int(limit), 10000))
        with self._db("market.db") as conn:
            table = self._market_table_for_code(conn, code)
            if table is None:
                return {
                    "ts_code": code,
                    "rows": [],
                    "count": 0,
                    "source_table": None,
                    "state": "NO_LEGACY_MARKET_DATA",
                    "provenance": LEGACY_PROVENANCE,
                }
            where = ["ts_code=?"]
            params: list[object] = [code]
            if start:
                where.append("trade_date>=?")
                params.append(start)
            if end:
                where.append("trade_date<=?")
                params.append(end)
            where_sql = " AND ".join(where)
            if table == "bars":
                columns = (
                    "ts_code,trade_date,open,high,low,close,pre_close,vol,amount,"
                    "adj_factor,up_limit,down_limit,limit_source,is_suspended,is_st,board"
                )
            else:
                columns = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
            rows = self._rows(
                conn,
                f"SELECT {columns} FROM {table} WHERE {where_sql} "
                "ORDER BY trade_date DESC LIMIT ?",
                (*params, limit),
            )
            rows.reverse()
        for row in rows:
            row["ts_code"] = _code(row.get("ts_code"))
            row["trade_date"] = _date(row.get("trade_date"))
            row["provenance"] = LEGACY_PROVENANCE
            row["publication_state"] = "LEGACY_EVIDENCE"
            row["source_table"] = table
            row["realtime_semantics"] = False
        dates = [str(row["trade_date"]) for row in rows if row.get("trade_date")]
        row_hash = _hash(rows)
        publication = self.publication(
            f"legacy.{table}.{code}",
            "market.db",
            table,
            row_count=len(rows),
            symbol_count=1 if rows else 0,
            start_date=dates[0] if dates else start,
            end_date=dates[-1] if dates else end,
            quality="LEGACY_UNVERIFIED",
        )
        return {
            "ts_code": code,
            "rows": rows,
            "data": rows,
            "count": len(rows),
            "available_dates": dates,
            "date_range": {"start": dates[0], "end": dates[-1]} if dates else None,
            "source_table": table,
            "source": self._source("market.db", table, row_hash=row_hash),
            "publication": publication,
            "manifest_hash": publication["manifest_hash"],
            "state": "LEGACY_READONLY_HISTORICAL" if rows else "NO_LEGACY_MARKET_DATA",
            "provenance": LEGACY_PROVENANCE,
            "realtime_semantics": False,
            "price_basis": "LEGACY_REPORTED_PRICE_UNVERIFIED_FOR_V2_EXECUTION",
        }

    def benchmarks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db("market.db") as conn:
            rows = self._rows(
                conn,
                "SELECT ts_code,count(*) rows,min(trade_date) start,max(trade_date) end "
                "FROM bench_daily GROUP BY ts_code ORDER BY ts_code LIMIT ?",
                (max(1, min(int(limit), 500)),),
            )
        return [
            {
                "ts_code": _code(row["ts_code"]),
                "rows": int(row["rows"] or 0),
                "start": _date(row["start"]),
                "end": _date(row["end"]),
                "provenance": LEGACY_PROVENANCE,
                "publication_state": "LEGACY_EVIDENCE",
            }
            for row in rows
        ]

    def strategies(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT * FROM strategy_registry ORDER BY strategy_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                strategy_id = _text(row.get("strategy_id"))
                evaluation = self._one(
                    conn,
                    "SELECT * FROM evaluations WHERE strategy_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (strategy_id,),
                )
                backtest_count = int(
                    conn.execute(
                        "SELECT count(*) FROM backtests WHERE strategy_id=?",
                        (strategy_id,),
                    ).fetchone()[0]
                )
                item = dict(row)
                item["params"] = _json(item.get("params_json"), {})
                item["factors"] = _json(item.get("factors_json"), [])
                item["legacy_status"] = "LEGACY_IMPORTED"
                item["research_status"] = "RESEARCH_ONLY"
                item["paper_eligible"] = False
                item["block_reasons"] = [
                    "LEGACY_RESULT_REQUIRES_V2_REVALIDATION",
                    "LEGACY_DATA_IS_NOT_V2_PAPER_QUALIFICATION",
                ]
                item["evaluation"] = self._normalize_evaluation(evaluation)
                item["backtest_count"] = backtest_count
                item["provenance"] = LEGACY_PROVENANCE
                item["source"] = self._source(
                    "lab.db", "strategy_registry", row_hash=_hash(item)
                )
                item["publication"] = self.publication(
                    "legacy.strategy_registry",
                    "lab.db",
                    "strategy_registry",
                    row_count=len(rows),
                    symbol_count=len(rows),
                    quality="LEGACY_UNVERIFIED",
                )
                result.append(item)
        return result

    def strategy_detail(self, strategy_id: str) -> dict[str, Any] | None:
        sid = _text(strategy_id)
        item = next((x for x in self.strategies(500) if x["strategy_id"] == sid), None)
        if item is None:
            return None
        with self._db("lab.db") as conn:
            evaluations = [
                self._normalize_evaluation(row)
                for row in self._rows(
                    conn,
                    "SELECT * FROM evaluations WHERE strategy_id=? ORDER BY created_at DESC",
                    (sid,),
                )
            ]
            backtests = self._backtests_for_strategy(conn, sid, 200)
            queue = self._rows(
                conn,
                "SELECT * FROM research_queue WHERE target=? OR strategy_id=? "
                "ORDER BY priority,created_at LIMIT 100",
                (sid, sid),
            )
        item["evaluations"] = evaluations
        item["backtests"] = backtests
        item["research_queue"] = queue
        item["detail_state"] = "LEGACY_READONLY_DETAIL"
        return item

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

    def factors(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            registry = self._rows(
                conn,
                "SELECT * FROM factor_registry ORDER BY factor_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            )
            result: list[dict[str, Any]] = []
            for row in registry:
                factor_id = _text(row.get("factor_id"))
                stats = self._one(
                    conn,
                    "SELECT * FROM factor_stats WHERE factor_id=? "
                    "ORDER BY CASE WHEN scope='research' THEN 0 ELSE 1 END LIMIT 1",
                    (factor_id,),
                )
                queue_row = self._one(
                    conn,
                    "SELECT * FROM research_queue WHERE target=? ORDER BY created_at DESC LIMIT 1",
                    (factor_id,),
                )
                item = dict(row)
                item["params"] = _json(item.get("params_json"), {})
                item["stats"] = dict(stats) if stats else {}
                item["result"] = _json(queue_row.get("result_json"), {}) if queue_row else {}
                direction = (
                    item["params"].get("direction")
                    if isinstance(item["params"], dict)
                    else None
                )
                item["direction"] = _text(direction, "UNKNOWN")
                item["legacy_status"] = "LEGACY_IMPORTED"
                item["research_status"] = "RESEARCH_ONLY"
                item["accepted"] = False
                item["paper_eligible"] = False
                item["block_reasons"] = [
                    "LEGACY_FACTOR_RESULT",
                    "V2_FACTOR_REVALIDATION_REQUIRED",
                ]
                item["provenance"] = LEGACY_PROVENANCE
                item["source"] = self._source(
                    "lab.db", "factor_registry", row_hash=_hash(item)
                )
                result.append(item)
        return result

    def factor_detail(self, factor_id: str, limit: int = 250) -> dict[str, Any] | None:
        fid = _text(factor_id)
        item = next((x for x in self.factors(500) if x["factor_id"] == fid), None)
        if item is None:
            return None
        with self._db("lab.db") as conn:
            stats = self._rows(
                conn,
                "SELECT * FROM factor_stats WHERE factor_id=? ORDER BY scope",
                (fid,),
            )
            ic = self._rows(
                conn,
                "SELECT * FROM factor_ic_daily WHERE factor_id=? "
                "ORDER BY trade_date DESC LIMIT ?",
                (fid, max(1, min(int(limit), 1000))),
            )
            coverage = self._one(
                conn,
                "SELECT count(*) rows,min(trade_date) start,max(trade_date) end,"
                "count(DISTINCT ts_code) symbols FROM features WHERE factor_id=?",
                (fid,),
            )
        coverage = coverage or {}
        item.update(
            {
                "stats_history": stats,
                "ic_daily": ic,
                "coverage": {
                    "rows": int(coverage.get("rows") or 0),
                    "symbols": int(coverage.get("symbols") or 0),
                    "start": _date(coverage.get("start")),
                    "end": _date(coverage.get("end")),
                },
                "detail_state": "LEGACY_READONLY_DETAIL",
                "research_evidence": item.get("source"),
            }
        )
        return item

    def _backtests_for_strategy(
        self, conn: sqlite3.Connection, strategy_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        params: list[object] = []
        where = ""
        if strategy_id:
            where = "WHERE b.strategy_id=?"
            params.append(strategy_id)
        params.append(max(1, min(int(limit), 500)))
        rows = self._rows(
            conn,
            "SELECT b.*,count(n.trade_date) n_days,"
            "min(n.trade_date) nav_start,max(n.trade_date) nav_end,"
            "min(n.nav) first_nav "
            "FROM backtests b LEFT JOIN bt_nav n ON n.bt_id=b.bt_id "
            f"{where} GROUP BY b.bt_id ORDER BY b.created_at DESC LIMIT ?",
            params,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            bt_id = _text(row.get("bt_id"))
            first = self._one(
                conn,
                "SELECT nav,bench_nav FROM bt_nav WHERE bt_id=? ORDER BY trade_date LIMIT 1",
                (bt_id,),
            )
            last = self._one(
                conn,
                "SELECT nav,bench_nav FROM bt_nav WHERE bt_id=? ORDER BY trade_date DESC LIMIT 1",
                (bt_id,),
            )
            first_nav = (
                float(first["nav"])
                if first and first.get("nav") is not None
                else None
            )
            last_nav = (
                float(last["nav"])
                if last and last.get("nav") is not None
                else None
            )
            first_bench = (
                float(first["bench_nav"])
                if first and first.get("bench_nav") is not None
                else None
            )
            last_bench = (
                float(last["bench_nav"])
                if last and last.get("bench_nav") is not None
                else None
            )
            item = dict(row)
            item["params"] = _json(item.get("params_json"), {})
            item["strategy_name"] = self._strategy_name(
                conn, _text(row.get("strategy_id"))
            )
            item["total_return"] = (
                last_nav / first_nav - 1 if first_nav and last_nav is not None else None
            )
            item["bench_return"] = (
                last_bench / first_bench - 1
                if first_bench and last_bench is not None
                else None
            )
            item["legacy_status"] = LEGACY_RESULT
            item["v2_revalidated"] = False
            item["paper_eligible"] = False
            item["provenance"] = LEGACY_PROVENANCE
            item["source"] = self._source(
                "lab.db", "backtests", row_hash=_hash(item)
            )
            out.append(item)
        return out

    @staticmethod
    def _strategy_name(conn: sqlite3.Connection, strategy_id: str) -> str:
        row = conn.execute(
            "SELECT name FROM strategy_registry WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        return _text(row[0]) if row else strategy_id

    def backtests(
        self, strategy_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            return self._backtests_for_strategy(conn, strategy_id, limit)

    def backtest_detail(self, backtest_id: str) -> dict[str, Any] | None:
        bid = _text(backtest_id)
        with self._db("lab.db") as conn:
            items = self._backtests_for_strategy(conn, None, 500)
            item = next((x for x in items if x["bt_id"] == bid), None)
            if item is None:
                return None
            trade_count = int(
                conn.execute(
                    "SELECT count(*) FROM bt_trades WHERE bt_id=?", (bid,)
                ).fetchone()[0]
            )
            order_count = int(
                conn.execute(
                    "SELECT count(*) FROM bt_orders WHERE bt_id=?", (bid,)
                ).fetchone()[0]
            )
            validation = self._rows(
                conn,
                "SELECT * FROM validation_runs WHERE bt_id=? ORDER BY created_at DESC",
                (bid,),
            )
            item.update(
                {
                    "trade_count": trade_count,
                    "order_count": order_count,
                    "validation": validation,
                    "metrics": self._backtest_metrics(conn, bid),
                    "detail_state": "LEGACY_READONLY_DETAIL",
                }
            )
            return item

    def _backtest_metrics(
        self, conn: sqlite3.Connection, backtest_id: str
    ) -> dict[str, Any]:
        rows = self._rows(
            conn,
            "SELECT trade_date,nav,ret,bench_nav,bench_ret,exposure FROM bt_nav "
            "WHERE bt_id=? ORDER BY trade_date",
            (backtest_id,),
        )
        navs = [float(row["nav"]) for row in rows if row.get("nav") is not None]
        rets = [float(row["ret"]) for row in rows if row.get("ret") is not None]
        if not navs:
            return {"n_days": 0}
        first = navs[0]
        peak = navs[0]
        max_dd = 0.0
        for value in navs:
            peak = max(peak, value)
            max_dd = max(max_dd, 1 - value / peak)
        mean = sum(rets) / len(rets) if rets else 0.0
        variance = (
            sum((value - mean) ** 2 for value in rets) / (len(rets) - 1)
            if len(rets) > 1
            else 0.0
        )
        std = variance**0.5
        exposures = [
            float(row["exposure"]) for row in rows if row.get("exposure") is not None
        ]
        return {
            "n_days": len(navs),
            "total_return": navs[-1] / first - 1 if first else None,
            "max_drawdown": max_dd,
            "sharpe_annualized_244": mean / std * (244**0.5) if std else None,
            "average_exposure": sum(exposures) / len(exposures) if exposures else None,
            "legacy_metric_convention": "LEGACY_RESULT; NOT V2_CANONICAL",
        }

    def backtest_nav(
        self, backtest_id: str, limit: int = 5000
    ) -> dict[str, Any] | None:
        detail = self.backtest_detail(backtest_id)
        if detail is None:
            return None
        bid = _text(backtest_id)
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT trade_date,nav,ret,bench_nav,bench_ret,exposure "
                "FROM bt_nav WHERE bt_id=? ORDER BY trade_date LIMIT ?",
                (bid, max(1, min(int(limit), 10000))),
            )
        nav_values = [float(row["nav"]) for row in rows if row.get("nav") is not None]
        bench_values = [
            float(row["bench_nav"])
            for row in rows
            if row.get("bench_nav") is not None
        ]
        nav0 = nav_values[0] if nav_values else None
        bench0 = bench_values[0] if bench_values else None
        peak = nav0 or 0.0
        series: list[dict[str, Any]] = []
        for row in rows:
            nav = float(row["nav"]) if row.get("nav") is not None else None
            if nav is not None:
                peak = max(peak, nav)
            series.append(
                {
                    **row,
                    "nav_normalized": nav / nav0 if nav is not None and nav0 else None,
                    "bench_normalized": (
                        float(row["bench_nav"]) / bench0
                        if row.get("bench_nav") is not None and bench0
                        else None
                    ),
                    "drawdown": 1 - nav / peak if nav is not None and peak else None,
                    "provenance": LEGACY_PROVENANCE,
                }
            )
        return {
            "backtest_id": bid,
            "bt_id": bid,
            "strategy_id": detail["strategy_id"],
            "strategy_name": detail["strategy_name"],
            "series": series,
            "rows": series,
            "summary": detail["metrics"],
            "legacy_status": LEGACY_RESULT,
            "publication": self.publication(
                f"legacy.backtest.{bid}.nav",
                "lab.db",
                "bt_nav",
                row_count=len(series),
                symbol_count=1,
                start_date=series[0].get("trade_date") if series else None,
                end_date=series[-1].get("trade_date") if series else None,
                quality="LEGACY_UNVERIFIED",
            ),
            "source": self._source("lab.db", "bt_nav", row_hash=_hash(series)),
        }

    def backtest_trades(
        self, backtest_id: str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT * FROM bt_trades WHERE bt_id=? ORDER BY trade_date,seq LIMIT ?",
                (str(backtest_id), max(1, min(int(limit), 10000))),
            )
        for row in rows:
            row.update(
                {
                    "trade_id": f"{backtest_id}:{row.get('seq')}",
                    "provenance": LEGACY_PROVENANCE,
                    "legacy_status": LEGACY_RESULT,
                    "source_table": "bt_trades",
                }
            )
        return rows

    def backtest_fills(
        self, backtest_id: str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self.backtest_trades(backtest_id, limit)

    def backtest_orders(
        self, backtest_id: str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT * FROM bt_orders WHERE bt_id=? ORDER BY issued_date,seq LIMIT ?",
                (str(backtest_id), max(1, min(int(limit), 10000))),
            )
        for row in rows:
            row.update(
                {
                    "order_id": f"{backtest_id}:{row.get('seq')}",
                    "provenance": LEGACY_PROVENANCE,
                    "legacy_status": LEGACY_RESULT,
                    "source_table": "bt_orders",
                }
            )
        return rows

    def research_queue(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT * FROM research_queue ORDER BY status,priority,created_at,item_id LIMIT ?",
                (max(1, min(int(limit), 500)),),
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            state = _text(row.get("status") or row.get("state"), "UNKNOWN")
            item = dict(row)
            item["status"] = state
            item["legacy_state"] = state
            item["missing_dependencies"] = ["LEGACY_DATA_NOT_V2_CANONICAL"]
            item["dataset"] = "legacy.quant3"
            item["oos_status"] = "LEGACY_EVIDENCE_ONLY"
            item["current_step"] = "READONLY_IMPORT"
            item["runtime_job"] = None
            item["last_error"] = row.get("last_error") or row.get("reason")
            item["run_allowed"] = False
            item["block_reasons"] = [
                "LEGACY_QUEUE_CANNOT_EXECUTE_DIRECTLY",
                "V2_RUN_REQUIRES_CURRENT_GATE",
            ]
            item["provenance"] = LEGACY_PROVENANCE
            result.append(item)
        return result

    def paper_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db("lab.db") as conn:
            rows = self._rows(
                conn,
                "SELECT * FROM paper_account ORDER BY strategy_id LIMIT ?",
                (max(1, min(int(limit), 200)),),
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                sid = _text(row.get("strategy_id"))
                positions = self._rows(
                    conn,
                    "SELECT * FROM paper_positions WHERE strategy_id=? AND shares>0 "
                    "ORDER BY shares*coalesce(last_price,0) DESC",
                    (sid,),
                )
                nav = self._rows(
                    conn,
                    "SELECT * FROM paper_nav WHERE strategy_id=? ORDER BY trade_date",
                    (sid,),
                )
                signals = self._rows(
                    conn,
                    "SELECT * FROM paper_signals WHERE strategy_id=? "
                    "ORDER BY signal_date DESC LIMIT 100",
                    (sid,),
                )
                orders = self._rows(
                    conn,
                    "SELECT * FROM paper_orders WHERE strategy_id=? "
                    "ORDER BY order_date DESC LIMIT 100",
                    (sid,),
                )
                fills = self._rows(
                    conn,
                    "SELECT * FROM paper_fills WHERE strategy_id=? "
                    "ORDER BY trade_date DESC LIMIT 100",
                    (sid,),
                )
                item = dict(row)
                item.update(
                    {
                        "status": LEGACY_PAPER,
                        "legacy_status": LEGACY_PAPER,
                        "paper_eligible": False,
                        "execution_allowed": False,
                        "block_reasons": [
                            "LEGACY_PAPER_READONLY",
                            "CURRENT_V2_QUALIFICATION_REQUIRED",
                        ],
                        "positions": positions,
                        "nav_series": nav,
                        "signals": [
                            self._decode_json_field(x, "payload_json") for x in signals
                        ],
                        "orders": orders,
                        "fills": fills,
                        "ledger": [],
                        "reconciliation_status": "LEGACY_READONLY_NOT_RECONCILED_IN_V2",
                        "provenance": LEGACY_PROVENANCE,
                        "source": self._source(
                            "lab.db", "paper_account", row_hash=_hash(item)
                        ),
                    }
                )
                result.append(item)
        return result

    @staticmethod
    def _decode_json_field(row: dict[str, Any], field: str) -> dict[str, Any]:
        value = dict(row)
        value[field.removesuffix("_json")] = _json(
            value.get(field), value.get(field)
        )
        return value

    def paper_detail(self, strategy_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.paper_accounts(200)
                if item["strategy_id"] == strategy_id
            ),
            None,
        )

    def events(self, limit: int = 100, kind: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._db("lab.db") as conn:
            params: list[object] = []
            where = "1=1"
            if kind:
                where = "e.kind=?"
                params.append(kind)
            rows = self._rows(
                conn,
                "SELECT e.event_id,e.ts_code,e.company_name,e.kind,e.score,e.event_ts,"
                "e.event_date,e.source,e.title,e.ref_id,e.created_at "
                f"FROM event_store e WHERE {where} "
                "ORDER BY e.event_date DESC,e.event_id LIMIT ?",
                (*params, limit),
            )
            stats = self._rows(
                conn,
                "SELECT * FROM event_stats WHERE scope='research' ORDER BY n_events DESC",
            )
            total = int(conn.execute("SELECT count(*) FROM event_store").fetchone()[0])
        for row in rows:
            row.update(
                {
                    "event_type": row.get("kind"),
                    "published_at": _date(row.get("event_date")),
                    "summary": f"{row.get('company_name') or row.get('ts_code') or ''} · {row.get('kind') or ''}",
                    "source_kind": LEGACY_EVENT,
                    "provenance": LEGACY_PROVENANCE,
                    "publication_state": "LEGACY_EVIDENCE",
                    "evidence_ref": f"legacy://lab.db/event_store/{row.get('event_id')}",
                }
            )
        return {
            "events": rows,
            "count": total,
            "kind_stats": stats,
            "state": "LEGACY_HISTORY_AVAILABLE",
            "live_vendor": "PENDING_VENDOR",
            "event_engine_ready": True,
            "provenance": LEGACY_PROVENANCE,
            "publication": self.publication(
                "legacy.event_store",
                "lab.db",
                "event_store",
                row_count=total,
                symbol_count=None,
                quality="LEGACY_UNVERIFIED",
            ),
        }

    def data_health(self) -> dict[str, Any]:
        with self._db("market.db") as conn:
            rows = self._rows(conn, "SELECT * FROM data_health ORDER BY dataset")
        coverage: dict[str, Any] = {}
        for row in rows:
            dataset = _text(row.get("dataset"))
            coverage[dataset] = {
                "dataset": dataset,
                "source": LEGACY_SOURCE_ID,
                "source_kind": LEGACY_PROVENANCE,
                "publication_state": "LEGACY_EVIDENCE",
                "total_rows": int(row.get("rows_total") or 0),
                "row_count": int(row.get("rows_total") or 0),
                "earliest_date": _date(row.get("min_date")),
                "latest_date": _date(row.get("max_date")),
                "quality_score": row.get("quality_score"),
                "status": _text(row.get("status"), "UNKNOWN"),
                "stale_days": row.get("stale_days"),
                "detail": row.get("detail"),
            }
        return {
            "overall_source": LEGACY_SOURCE_ID,
            "provenance_status": LEGACY_PROVENANCE,
            "coverage": coverage,
            "catalog": list(coverage.values()),
            "watermarks": [],
            "recent_ingestions": 0,
            "recent_errors": [],
            "message": "Legacy data is exposed lazily and read-only; it is not copied into control.sqlite.",
        }

    def predict(
        self, ts_code: str, start_date: str = "", end_date: str = ""
    ) -> dict[str, Any]:
        """Return an explicitly research-only historical diagnostic."""
        daily = self.daily(ts_code, start_date, end_date, limit=500)
        rows = daily["rows"]
        closes = [float(row["close"]) for row in rows if row.get("close") is not None]

        def ret(n: int) -> float | None:
            if len(closes) <= n or closes[-n - 1] == 0:
                return None
            return closes[-1] / closes[-n - 1] - 1

        r5, r20 = ret(5), ret(20)
        recent = [
            closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]
        ][-20:]
        mean = sum(recent) / len(recent) if recent else 0
        variance = (
            sum((x - mean) ** 2 for x in recent) / (len(recent) - 1)
            if len(recent) > 1
            else 0
        )
        return {
            "ts_code": _code(ts_code),
            "last_date": rows[-1].get("trade_date") if rows else None,
            "last_close": closes[-1] if closes else None,
            "return_5d": r5,
            "return_20d": r20,
            "volatility_20d": variance**0.5 if variance else None,
            "historical_regime": (
                "UPTREND"
                if r20 is not None and r20 > 0.05
                else "DOWNTREND"
                if r20 is not None and r20 < -0.05
                else "MIXED"
            ),
            "research_only": True,
            "experimental": True,
            "trading_signal": False,
            "paper_eligible": False,
            "label": "EXPERIMENTAL / RESEARCH_ONLY",
            "provenance": daily.get("provenance", LEGACY_PROVENANCE),
            "source": daily.get("source"),
        }

    def readonly_check(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        for database in ("market.db", "lab.db"):
            try:
                with self._db(database) as conn:
                    query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
                    checks.append(
                        {
                            "database": database,
                            "exists": True,
                            "query_only": query_only,
                            "read_only": query_only == 1,
                            "mutation_attempted": False,
                        }
                    )
            except Exception as exc:
                checks.append(
                    {
                        "database": database,
                        "exists": False,
                        "read_only": False,
                        "mutation_attempted": False,
                        "error": str(exc),
                    }
                )
        return {
            "status": "PASS"
            if checks and all(x.get("read_only") for x in checks)
            else "FAIL",
            "checks": checks,
            "legacy_write_attempted": False,
            "policy": "READ_ONLY_FOREVER",
            "generated_at": datetime.now(UTC).isoformat(),
        }


def legacy_adapter(root: str | Path) -> LegacyReadonlyAdapter:
    return LegacyReadonlyAdapter(root)
