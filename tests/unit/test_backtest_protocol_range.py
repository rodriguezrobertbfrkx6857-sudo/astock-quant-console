from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from quantweb.v2_api import _backtest_range_reason
from src.quantcore.research.price_executor import _frozen_protocol_range


def test_frozen_protocol_range_uses_train_start_and_final_validation_end() -> None:
    protocol = SimpleNamespace(
        train_window=("2019-06-26", "2020-12-31"),
        validation_windows=(
            ("2021-01-01", "2021-12-31"),
            ("2023-01-01", "2024-12-31"),
        ),
    )

    assert _frozen_protocol_range(protocol) == (
        date(2019, 6, 26),
        date(2024, 12, 31),
    )


def test_api_blocks_range_that_would_change_frozen_stage09_scope() -> None:
    assert _backtest_range_reason("20190626", "20241231") is None
    reason = _backtest_range_reason("20190626", "20250818")
    assert reason is not None
    assert "blocked instead of silently changing" in reason
