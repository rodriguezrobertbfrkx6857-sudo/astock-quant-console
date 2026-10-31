"""Partitioned immutable market-data plane for large price-only research sets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.dataset as pads  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from src.quantcore.contracts.safety import PathBoundary
from src.quantcore.data.hashing import canonical_json_bytes


@dataclass(frozen=True)
class PartitionEvidence:
    dataset: str
    month: str
    relative_path: str
    sha256: str
    rows: int
    assets: int
    min_date: str
    max_date: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8"
    )
    os.replace(temporary, path)


def _catalog(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_dataset_versions(
          version_id TEXT PRIMARY KEY,
          profile TEXT NOT NULL,
          manifest_hash TEXT NOT NULL UNIQUE,
          manifest_path TEXT NOT NULL,
          start_date TEXT NOT NULL,
          end_date TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          asset_count INTEGER NOT NULL,
          quality_status TEXT NOT NULL,
          source_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS market_publications(
          profile TEXT PRIMARY KEY,
          version_id TEXT NOT NULL REFERENCES market_dataset_versions(version_id),
          manifest_hash TEXT NOT NULL,
          published_at TEXT NOT NULL
        );
        """
    )
    return conn


class PartitionedMarketStore:
    def __init__(self, root: Path, catalog_path: Path) -> None:
        self.root = PathBoundary().require_writable(root)
        self.catalog_path = PathBoundary().require_writable(catalog_path)
        self.root.mkdir(parents=True, exist_ok=True)

    def build_from_legacy_raw(
        self,
        *,
        profile: str,
        source: Path,
        start_date: str,
        end_date: str,
        datasets: tuple[str, ...] = ("daily", "stk_limit"),
        reserve_bytes: int,
    ) -> dict[str, Any]:
        source = PathBoundary().require_legacy_readonly(source)
        source_stat = source.stat()
        request = {
            "profile": profile,
            "source": str(source),
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "start_date": start_date,
            "end_date": end_date,
            "datasets": datasets,
            "schema": "partitioned-market-store.v1",
        }
        request_hash = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
        version_id = f"market-{request_hash[:24]}"
        version_root = self.root / "versions" / version_id
        existing_manifest = version_root / "manifest.json"
        if existing_manifest.is_file():
            manifest = json.loads(existing_manifest.read_text("utf-8"))
            if manifest.get("request_hash") != request_hash:
                raise RuntimeError("immutable market-store version conflict")
            return {**manifest, "status": "ALREADY_PUBLISHED"}

        free_bytes = shutil.disk_usage(self.root).free
        conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        estimates: dict[str, int] = {}
        for dataset in datasets:
            estimates[dataset] = int(
                conn.execute(
                    "SELECT coalesce(sum(row_count),0) FROM raw_meta WHERE api=? "
                    "AND json_extract(params_json,'$.trade_date') BETWEEN ? AND ? "
                    "AND status='ok'",
                    (dataset, start_date, end_date),
                ).fetchone()[0]
            )
        estimated_output = sum(estimates.values()) * 72
        if free_bytes - estimated_output < reserve_bytes:
            conn.close()
            raise RuntimeError(
                f"BLOCKED_RESOURCE: free={free_bytes}, estimated={estimated_output}, reserve={reserve_bytes}"
            )

        building = self.root / "_building" / version_id
        building.mkdir(parents=True, exist_ok=False)
        partitions: list[PartitionEvidence] = []
        source_batches: dict[str, list[str]] = {}
        total_rows = 0
        assets: set[str] = set()
        try:
            for dataset in datasets:
                months = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT substr(trade_date,1,6) FROM raw_rows INDEXED BY ix_raw_rows_api_date "
                        "WHERE api=? AND trade_date BETWEEN ? AND ? ORDER BY 1",
                        (dataset, start_date, end_date),
                    )
                    if row[0]
                ]
                source_batches[dataset] = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT batch_id FROM raw_meta WHERE api=? AND status='ok' "
                        "AND json_extract(params_json,'$.trade_date') BETWEEN ? AND ? ORDER BY batch_id",
                        (dataset, start_date, end_date),
                    )
                ]
                for month in months:
                    raw_rows = conn.execute(
                        "SELECT payload_json FROM raw_rows INDEXED BY ix_raw_rows_api_date "
                        "WHERE api=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date,ts_code,seq",
                        (dataset, max(start_date, month + "01"), min(end_date, month + "31")),
                    )
                    rows: list[dict[str, Any]] = []
                    seen: dict[tuple[str, str], bytes] = {}
                    for raw_row in raw_rows:
                        item = json.loads(str(raw_row[0]))
                        key = (str(item.get("ts_code")), str(item.get("trade_date")))
                        body = canonical_json_bytes(item)
                        previous = seen.get(key)
                        if previous == body:
                            continue
                        if previous is not None:
                            raise RuntimeError(
                                f"SOURCE_CONFLICT:{dataset}:{key[0]}:{key[1]}"
                            )
                        seen[key] = body
                        trade_date = key[1]
                        item["available_at"] = (
                            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T17:00:00+08:00"
                        )
                        item["source_api"] = dataset
                        rows.append(item)
                    if not rows:
                        continue
                    codes = {str(row["ts_code"]) for row in rows}
                    assets.update(codes)
                    target = (
                        building
                        / f"dataset={dataset}"
                        / f"year={month[:4]}"
                        / f"month={month[4:]}"
                        / "part-00000.parquet"
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    table = pa.Table.from_pylist(rows)
                    pq.write_table(
                        table,
                        target,
                        compression="zstd",
                        compression_level=9,
                        use_dictionary=True,
                        write_statistics=True,
                    )
                    digest = _sha256(target)
                    evidence = PartitionEvidence(
                        dataset=dataset,
                        month=month,
                        relative_path=target.relative_to(building).as_posix(),
                        sha256=digest,
                        rows=len(rows),
                        assets=len(codes),
                        min_date=min(str(row["trade_date"]) for row in rows),
                        max_date=max(str(row["trade_date"]) for row in rows),
                    )
                    partitions.append(evidence)
                    total_rows += len(rows)
        except Exception:
            shutil.rmtree(building, ignore_errors=True)
            raise
        finally:
            conn.close()

        if not partitions or total_rows <= 0:
            shutil.rmtree(building, ignore_errors=True)
            raise RuntimeError("market-store build produced no partitions")
        lineage_hash = hashlib.sha256(canonical_json_bytes(source_batches)).hexdigest()
        manifest_body: dict[str, Any] = {
            "profile": profile,
            "version_id": version_id,
            "request_hash": request_hash,
            "schema_version": "partitioned-market-store.v1",
            "provenance": "REAL",
            "quality_status": "PASS",
            "source_access": "READ_ONLY_IMMUTABLE_RAW",
            "source_database": str(source),
            "source_fingerprint": request_hash,
            "source_batch_lineage_hash": lineage_hash,
            "start_date": min(item.min_date for item in partitions),
            "end_date": max(item.max_date for item in partitions),
            "row_count": total_rows,
            "asset_count": len(assets),
            "available_at_policy": "CONSERVATIVE_EOD_17_00_ASIA_SHANGHAI",
            "datasets": list(datasets),
            "partitions": [item.__dict__ for item in partitions],
            "created_at": datetime.now(UTC).isoformat(),
            "synthetic_fallback": False,
        }
        manifest_hash = hashlib.sha256(canonical_json_bytes(manifest_body)).hexdigest()
        manifest = {**manifest_body, "manifest_hash": manifest_hash}
        _atomic_json(building / "manifest.json", manifest)
        version_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(building, version_root)
        pointer = {
            "profile": profile,
            "version_id": version_id,
            "manifest_hash": manifest_hash,
            "manifest_path": str(version_root / "manifest.json"),
            "published_at": datetime.now(UTC).isoformat(),
        }
        _atomic_json(self.root / "published" / f"{profile}.json", pointer)
        catalog = _catalog(self.catalog_path)
        try:
            catalog.execute("BEGIN IMMEDIATE")
            catalog.execute(
                "INSERT OR IGNORE INTO market_dataset_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    profile,
                    manifest_hash,
                    str(version_root / "manifest.json"),
                    manifest["start_date"],
                    manifest["end_date"],
                    total_rows,
                    len(assets),
                    "PASS",
                    request_hash,
                    manifest["created_at"],
                ),
            )
            catalog.execute(
                "INSERT INTO market_publications VALUES (?,?,?,?) ON CONFLICT(profile) DO UPDATE SET "
                "version_id=excluded.version_id,manifest_hash=excluded.manifest_hash,published_at=excluded.published_at",
                (profile, version_id, manifest_hash, pointer["published_at"]),
            )
            catalog.commit()
        except Exception:
            catalog.rollback()
            raise
        finally:
            catalog.close()
        return {**manifest, "status": "PASS"}

    def scan(
        self,
        *,
        profile: str,
        dataset: str,
        columns: Iterable[str] | None = None,
        symbols: Iterable[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pa.Table:
        pointer_path = self.root / "published" / f"{profile}.json"
        pointer = json.loads(pointer_path.read_text("utf-8"))
        dataset_root = self.root / "versions" / str(pointer["version_id"])
        source = pads.dataset(
            dataset_root / f"dataset={dataset}", format="parquet", partitioning="hive"
        )
        predicate: Any = None
        symbols_list = list(symbols or ())
        if symbols_list:
            predicate = pads.field("ts_code").isin(symbols_list)
        if start_date:
            part = pads.field("trade_date") >= start_date
            predicate = part if predicate is None else predicate & part
        if end_date:
            part = pads.field("trade_date") <= end_date
            predicate = part if predicate is None else predicate & part
        return source.to_table(columns=list(columns) if columns else None, filter=predicate)


__all__ = ["PartitionEvidence", "PartitionedMarketStore"]
