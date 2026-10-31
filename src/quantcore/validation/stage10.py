from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from datetime import UTC, datetime
from pathlib import Path

from src.quantcore.contracts.execution import DailySnapshot
from src.quantcore.contracts.safety import PathBoundary


class Stage10Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(frozen=True)
class Difference:
    on: str
    category: str
    key: str
    formal: object
    reference: object


@dataclass(frozen=True)
class Stage10Result:
    status: Stage10Status
    input_hash: str
    formal_hash: str
    reference_hash: str
    differences: tuple[Difference, ...]
    evidence_valid: bool


def _hash(snapshots: tuple[DailySnapshot, ...]) -> str:
    payload = json.dumps(
        [asdict(x) for x in snapshots],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _indexed(
    rows: tuple[object, ...], key_name: str, on: str, category: str, side: str
) -> tuple[dict[str, object], list[Difference]]:
    keys = [str(getattr(row, key_name)) for row in rows]
    differences = [
        Difference(
            on,
            f"duplicate_{category}",
            key,
            count if side == "formal" else 0,
            count if side == "reference" else 0,
        )
        for key, count in Counter(keys).items()
        if count > 1
    ]
    return {str(getattr(row, key_name)): row for row in rows}, differences


def compare_snapshots(
    formal: tuple[DailySnapshot, ...],
    reference: tuple[DailySnapshot, ...],
    *,
    input_hash: str,
    sufficient_data: bool = True,
    same_specification: bool = True,
) -> Stage10Result:
    formal_hash, reference_hash = _hash(formal), _hash(reference)
    if not sufficient_data:
        return Stage10Result(
            Stage10Status.INSUFFICIENT_DATA,
            input_hash,
            formal_hash,
            reference_hash,
            (),
            False,
        )
    if not same_specification:
        return Stage10Result(
            Stage10Status.NOT_COMPARABLE,
            input_hash,
            formal_hash,
            reference_hash,
            (),
            False,
        )
    differences: list[Difference] = []
    formal_days = {x.on: x for x in formal}
    reference_days = {x.on: x for x in reference}
    for on in sorted(set(formal_days) | set(reference_days)):
        left, right = formal_days.get(on), reference_days.get(on)
        if left is None or right is None:
            differences.append(
                Difference(on, "day", on, left is not None, right is not None)
            )
            continue
        for category, key_name in (("order", "order_id"), ("fill", "fill_id")):
            left_map, duplicate_left = _indexed(
                getattr(left, category + "s"), key_name, on, category, "formal"
            )
            right_map, duplicate_right = _indexed(
                getattr(right, category + "s"), key_name, on, category, "reference"
            )
            differences.extend(duplicate_left + duplicate_right)
            for key in sorted(set(left_map) | set(right_map)):
                if left_map.get(key) != right_map.get(key):
                    differences.append(
                        Difference(
                            on, category, key, left_map.get(key), right_map.get(key)
                        )
                    )
        for field in ("cash", "positions", "total_fees", "nav", "event_receipts"):
            if getattr(left, field) != getattr(right, field):
                differences.append(
                    Difference(
                        on, field, field, getattr(left, field), getattr(right, field)
                    )
                )
    status = Stage10Status.PASS if not differences else Stage10Status.FAIL
    return Stage10Result(
        status,
        input_hash,
        formal_hash,
        reference_hash,
        tuple(differences),
        status is Stage10Status.PASS,
    )


class Stage10Store:
    def __init__(self, database: str | Path):
        self.database = PathBoundary().require_writable(database)

    def record(self, research_run_id: str, result: Stage10Result) -> str:
        if not research_run_id:
            raise ValueError("research_run_id is required")
        identity = "|".join(
            (
                research_run_id,
                result.input_hash,
                result.formal_hash,
                result.reference_hash,
            )
        )
        result_id = hashlib.sha256(identity.encode()).hexdigest()
        conn = sqlite3.connect(self.database)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            values = (
                result_id,
                research_run_id,
                result.input_hash,
                result.formal_hash,
                result.reference_hash,
                result.status.value,
                int(result.evidence_valid),
            )
            existing = conn.execute(
                "SELECT result_id,research_run_id,input_hash,formal_hash,reference_hash,status,evidence_valid FROM stage10_runs WHERE result_id=?",
                (result_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise RuntimeError("immutable Stage10 result conflict")
                conn.rollback()
                return result_id
            conn.execute(
                "INSERT INTO stage10_runs VALUES (?,?,?,?,?,?,?,?)",
                values + (datetime.now(UTC).isoformat(),),
            )
            for number, difference in enumerate(result.differences):
                conn.execute(
                    "INSERT INTO stage10_differences VALUES (?,?,?,?,?,?,?)",
                    (
                        result_id,
                        number,
                        difference.on,
                        difference.category,
                        difference.key,
                        json.dumps(
                            difference.formal,
                            default=str,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            difference.reference,
                            default=str,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            conn.commit()
            return result_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
