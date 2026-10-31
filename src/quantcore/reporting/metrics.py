from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from src.quantcore.contracts.safety import PathBoundary


@dataclass(frozen=True)
class Metric:
    metric_id: str
    value: Decimal | None
    display_value: str
    unit: str
    convention: str
    version: str
    source_hash: str


class MetricCalculator:
    VERSION = "canonical-metrics-v1"
    PERIODS = Decimal(252)

    @staticmethod
    def _source_hash(nav: tuple[tuple[str, Decimal], ...]) -> str:
        return hashlib.sha256(
            json.dumps(
                [(day, format(value, "f")) for day, value in nav], separators=(",", ":")
            ).encode()
        ).hexdigest()

    def nav_metrics(self, nav: tuple[tuple[str, Decimal], ...]) -> tuple[Metric, ...]:
        source_hash = self._source_hash(nav)
        convention = "NAV_RATIO;DAILY;ANNUALIZATION=252;SAMPLE_STDDEV"
        if len(nav) < 2 or any(value <= 0 for _, value in nav):
            return tuple(
                Metric(name, None, "—", unit, convention, self.VERSION, source_hash)
                for name, unit in (
                    ("total_return", "PERCENT_POINT"),
                    ("annualized_return", "PERCENT_POINT"),
                    ("max_drawdown", "PERCENT_POINT"),
                    ("annualized_volatility", "PERCENT_POINT"),
                    ("sharpe", "RATIO"),
                )
            )
        values = [x[1] for x in nav]
        returns = [
            values[i] / values[i - 1] - Decimal(1) for i in range(1, len(values))
        ]
        total = values[-1] / values[0] - Decimal(1)
        years = Decimal(len(returns)) / self.PERIODS
        annual = (Decimal(1) + total) ** (Decimal(1) / years) - Decimal(1)
        peak = values[0]
        max_dd = Decimal(0)
        for value in values:
            peak = max(peak, value)
            max_dd = min(max_dd, value / peak - Decimal(1))
        mean = sum(returns, Decimal()) / Decimal(len(returns))
        variance = (
            sum(((x - mean) ** 2 for x in returns), Decimal())
            / Decimal(len(returns) - 1)
            if len(returns) > 1
            else Decimal(0)
        )
        daily_std = variance.sqrt()
        annual_vol = daily_std * self.PERIODS.sqrt()
        sharpe = (mean / daily_std * self.PERIODS.sqrt()) if daily_std else None

        def metric(name, value, unit):
            return Metric(
                name,
                value,
                (
                    f"{value*100:.2f}%"
                    if unit == "PERCENT_POINT"
                    else (f"{value:.2f}" if value is not None else "—")
                ),
                unit,
                convention,
                self.VERSION,
                source_hash,
            )

        return (
            metric("total_return", total, "PERCENT_POINT"),
            metric("annualized_return", annual, "PERCENT_POINT"),
            metric("max_drawdown", max_dd, "PERCENT_POINT"),
            metric("annualized_volatility", annual_vol, "PERCENT_POINT"),
            metric("sharpe", sharpe, "RATIO"),
        )


class ReportingStore:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def publish(
        self,
        publication_id: str,
        business_date: date,
        provenance: str,
        quality_status: str,
        input_hash: str,
        run_id: str,
        metrics: dict[tuple[str, str], tuple[Metric, ...]],
        evidence_ref: str,
    ) -> None:
        if provenance != "REAL" or quality_status != "PASS":
            raise ValueError("canonical publication requires REAL + PASS")
        now = datetime.now(UTC).isoformat()
        metric_rows = [
            (
                publication_id,
                entity_type,
                entity_id,
                item.metric_id,
                format(item.value, "f") if item.value is not None else None,
                item.display_value,
                item.unit,
                item.convention,
                item.version,
                item.source_hash,
                evidence_ref,
            )
            for (entity_type, entity_id), items in metrics.items()
            for item in items
        ]
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT business_date,provenance,quality_status,input_hash,run_id "
                "FROM reporting_publications WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            identity = (
                business_date.isoformat(),
                provenance,
                quality_status,
                input_hash,
                run_id,
            )
            if existing is not None:
                persisted_metrics = conn.execute(
                    "SELECT publication_id,entity_type,entity_id,metric_id,value,"
                    "display_value,unit,convention,metric_version,source_hash,evidence_ref "
                    "FROM canonical_metrics WHERE publication_id=? "
                    "ORDER BY entity_type,entity_id,metric_id",
                    (publication_id,),
                ).fetchall()
                if tuple(existing) != identity or sorted(persisted_metrics) != sorted(
                    metric_rows
                ):
                    raise RuntimeError("immutable reporting publication conflict")
                conn.execute(
                    "INSERT INTO current_reporting_publication VALUES (1,?,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET "
                    "publication_id=excluded.publication_id,updated_at=excluded.updated_at",
                    (publication_id, now),
                )
                conn.commit()
                return
            conn.execute(
                "INSERT INTO reporting_publications VALUES (?,?,?,?,?,?,?,?)",
                (
                    publication_id,
                    business_date.isoformat(),
                    now,
                    provenance,
                    quality_status,
                    input_hash,
                    run_id,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO canonical_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                metric_rows,
            )
            conn.execute(
                "INSERT INTO current_reporting_publication VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET publication_id=excluded.publication_id,updated_at=excluded.updated_at",
                (publication_id, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
