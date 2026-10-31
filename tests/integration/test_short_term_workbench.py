from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.quantcore.contracts.config import V2Config, V2Paths, V2Resources, V2Server
from src.quantcore.short_term.workbench import ShortTermWorkbench


def _workbench(tmp_path: Path) -> ShortTermWorkbench:
    state = tmp_path / "state"
    state.mkdir()
    db = state / "control.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(Path("src/quantcore/data/migrations/0017_short_term_workbench.sql").read_text("utf-8"))
    conn.close()
    cfg = V2Config(
        schema_version=2,
        paths=V2Paths(tmp_path / "data", state, tmp_path / "artifacts", tmp_path / "logs", tmp_path / "legacy"),
        server=V2Server("127.0.0.1", 8888),
        resources=V2Resources(0, 0),
        source_path=tmp_path / "settings.json",
    )
    workbench = ShortTermWorkbench.__new__(ShortTermWorkbench)
    workbench.cfg = cfg
    workbench.database = db
    workbench.manifests = None
    return workbench


def test_holding_revision_is_idempotent_and_enforces_t_plus_one(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    payload = {"asset_id": "600000.SH", "shares": 1000, "sellable_shares": 800, "today_bought_shares": 200, "cost_price": 10.25}
    first = workbench.save_holding(payload)
    second = workbench.save_holding(payload)
    assert first["revision_id"] == second["revision_id"]
    assert workbench.holdings()[0]["sellable_shares"] == 800
    with pytest.raises(ValueError, match=r"T\+1"):
        workbench.save_holding({**payload, "sellable_shares": 900})


def test_holding_revision_cannot_be_overwritten(tmp_path: Path) -> None:
    workbench = _workbench(tmp_path)
    row = workbench.save_holding({"asset_id": "000001.SZ", "shares": 100, "sellable_shares": 100, "cost_price": 9.5})
    conn = sqlite3.connect(workbench.database)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE manual_holding_revisions SET shares=0 WHERE revision_id=?", (row["revision_id"],))
    conn.close()


def test_price_rules_are_deterministic_and_not_probabilities() -> None:
    features = {"close": 10.0, "sma20": 9.9, "sma60": 9.5, "atr14": 0.2, "ret3": -0.01, "ret5": 0.01, "prior_high20": 10.5, "amount": 100_000.0, "median_amount20": 80_000.0}
    signals = ShortTermWorkbench._signals(features)
    assert [item[0] for item in signals] == ["SHORT_TREND_PULLBACK_V1"]
    assert signals[0][2]["stop"] == pytest.approx(9.8)
