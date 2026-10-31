from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.research.price_executor import _source_hash  # noqa: E402
from src.quantcore.research.queue import ResearchStore  # noqa: E402
from src.quantcore.research.registry import ResearchProtocol, StrategyVersion  # noqa: E402


def _snapshot(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _protocol(
    *,
    protocol_id: str,
    family_id: str,
    hypothesis: str,
    falsifier: str,
    direction: str,
    parameter_grid: tuple[tuple[str, tuple[object, ...]], ...],
    label_horizon: int,
    purge: int,
    embargo: int,
) -> ResearchProtocol:
    return ResearchProtocol(
        protocol_id=protocol_id,
        family_id=family_id,
        hypothesis=hypothesis,
        falsifier=falsifier,
        source_code_hash=_source_hash(),
        parameter_grid=parameter_grid,
        direction=direction,
        universe_version="ETF3_BOUNDED_V1",
        data_dependencies=("canonical_prices", "historical_etf_master", "execution_limits"),
        availability_policy="PIT_AVAILABLE_AT_SESSION_CLOSE_PLUS_PUBLICATION",
        rebalance_and_execution="MONTHLY_T_CLOSE_SIGNAL_NEXT_SESSION_OPEN",
        holding_constraints="LONG_ONLY_NO_LEVERAGE_ETF3_ONLY",
        cost_model="historical-rulebook-v1",
        train_window=("2019-06-26", "2020-12-31"),
        validation_windows=(
            ("2021-01-01", "2021-12-31"),
            ("2022-01-01", "2022-12-31"),
            ("2023-01-01", "2024-12-31"),
        ),
        label_horizon=label_horizon,
        purge_sessions=purge,
        embargo_sessions=embargo,
        controls=("CTRL_CASH", "ETF3_EQUAL_WEIGHT"),
        primary_metric="NET_EXCESS_RETURN_WITH_BLOCK_UNCERTAINTY",
        statistics_policy="TIME_BLOCK_CI_PARAMETER_NEIGHBORHOOD_COST_1X_2X",
        family_budget=6,
        stop_rule="ONE_FROZEN_RUN_NO_POST_RESULT_TUNING",
        oos_access="KNOWN_ONLY",
        publication_level="INTERNAL_RESEARCH",
        owner_version="integrated-v2.s14",
        candidate_threshold=Decimal("0.02"),
        control_only=False,
    )


def build() -> tuple[tuple[ResearchProtocol, StrategyVersion, str, int], ...]:
    definitions = (
        (
            _protocol(
                protocol_id="R01.ETF3.v2",
                family_id="ETF_ROTATION",
                hypothesis="Causal medium-horizon ETF momentum may persist after frozen costs.",
                falsifier="It does not beat frozen equal-weight ETF3 after costs in the validation blocks.",
                direction="POSITIVE",
                parameter_grid=(("lookback", (60, 120)), ("holding", (1,))),
                label_horizon=21,
                purge=21,
                embargo=1,
            ),
            "etf_rotation.v2",
            "queue-etf-rotation-v2",
            10,
        ),
        (
            _protocol(
                protocol_id="R02.ETF3_LOWVOL.v1",
                family_id="ETF_LOW_VOL",
                hypothesis="The least volatile ETF3 member may provide a lower-risk allocation after costs.",
                falsifier="The low-volatility selection has no stable risk or excess-return evidence.",
                direction="NEGATIVE_VOLATILITY",
                parameter_grid=(("window", (60,)), ("measure", ("standard",))),
                label_horizon=21,
                purge=21,
                embargo=1,
            ),
            "etf_lowvol.v1",
            "queue-etf-lowvol-v1",
            11,
        ),
        (
            _protocol(
                protocol_id="R05.ETF3_TREND.v1",
                family_id="ETF_TREND_DEFENSIVE",
                hypothesis="An ETF own-price trend filter can move to cash when all bounded ETF3 members are below trend.",
                falsifier="The causal trend switch does not improve drawdown or net excess return versus frozen equal weight.",
                direction="POSITIVE_WITH_CASH_SWITCH",
                parameter_grid=(("lookback", (120,)), ("cash_switch", ("below_sma",))),
                label_horizon=21,
                purge=21,
                embargo=1,
            ),
            "etf_trend_defensive.v1",
            "queue-etf-trend-v1",
            12,
        ),
        (
            _protocol(
                protocol_id="R06.ETF3_MOM20.v1",
                family_id="ETF_MOMENTUM_SHORT",
                hypothesis="A frozen 20-session ETF3 momentum ranking may persist after execution costs.",
                falsifier="The short-horizon ranking does not beat frozen equal weight after costs across validation blocks.",
                direction="POSITIVE",
                parameter_grid=(("lookback", (20,)), ("holding", (1,))),
                label_horizon=21,
                purge=21,
                embargo=1,
            ),
            "etf_momentum_20.v1",
            "queue-etf-momentum-20-v1",
            13,
        ),
        (
            _protocol(
                protocol_id="R07.ETF3_RISKADJ60.v1",
                family_id="ETF_RISK_ADJUSTED",
                hypothesis="A frozen 60-session return-to-volatility ETF3 ranking may improve risk-adjusted allocation after costs.",
                falsifier="The risk-adjusted ranking has no stable net advantage over frozen equal weight.",
                direction="POSITIVE",
                parameter_grid=(("lookback", (60,)), ("score", ("return_over_volatility",))),
                label_horizon=21,
                purge=21,
                embargo=1,
            ),
            "etf_risk_adjusted_60.v1",
            "queue-etf-risk-adjusted-60-v1",
            14,
        ),
    )
    result: list[tuple[ResearchProtocol, StrategyVersion, str, int]] = []
    for protocol, version_id, queue_id, priority in definitions:
        version = StrategyVersion(
            version_id,
            protocol.family_id,
            protocol.protocol_hash,
            protocol.source_code_hash,
            _snapshot(f"{version_id}:ETF3_BOUNDED_V1"),
            protocol.universe_version,
        )
        result.append((protocol, version, queue_id, priority))
    return tuple(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Register bounded real ETF3 price strategies")
    parser.add_argument("--apply", action="store_true", help="write protocol/version/queue rows")
    args = parser.parse_args()
    cfg = load_v2_config(create_dirs=True)
    store = ResearchStore(cfg.paths.state / "control.sqlite")
    inserted = []
    for protocol, version, queue_id, priority in build():
        if args.apply:
            store.register(protocol, version, queue_id, priority)
        inserted.append({
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.protocol_hash,
            "strategy_version_id": version.strategy_version_id,
            "queue_item_id": queue_id,
            "source_code_hash": version.source_code_hash,
        })
    report = {
        "status": "APPLIED" if args.apply else "DRY_RUN",
        "scope": "ETF3_BOUNDED_V1",
        "strategies": inserted,
        "legacy_write_attempted": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
