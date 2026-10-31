from __future__ import annotations

from scripts.v2.update_daily_publication import update_daily_publication
from src.quantcore.activation.public_eod import (
    PublicEODError,
    normalize_sina_payload,
    normalize_tencent_payload,
    normalize_tencent_quote_payload,
)


def test_tencent_accepts_only_unadjusted_day_and_preserves_unknowns() -> None:
    payload = {
        "data": {
            "sh600519": {
                "day": [
                    ["2026-09-10", "10", "11", "12", "9", "100"],
                    ["2026-09-11", "11", "12", "13", "10", "101"],
                ],
                "qfqday": [["2026-09-11", "1", "1", "1", "1", "1"]],
            }
        }
    }
    rows = normalize_tencent_payload(payload, "600519.SH", {"20260911"})
    assert len(rows) == 1
    assert rows[0]["close"] == 12.0
    assert rows[0]["pre_close"] == 11.0
    assert rows[0]["price_basis"] == "UNADJUSTED_PRICE_ONLY"
    assert rows[0]["volume_unit"] == "LOT_100_SHARES"
    for field in (
        "amount",
        "adj_factor",
        "up_limit",
        "down_limit",
        "is_suspended",
        "is_st",
        "board",
    ):
        assert rows[0][field] is None


def test_tencent_never_uses_qfq_fallback() -> None:
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [["2026-09-11", "1", "1", "1", "1", "1"]]
            }
        }
    }
    try:
        normalize_tencent_payload(payload, "600519.SH", {"20260911"})
    except PublicEODError as exc:
        assert "unadjusted" in str(exc)
    else:
        raise AssertionError("qfq-only response must fail closed")


def test_sina_marks_volume_unit_and_unknown_execution_fields() -> None:
    payload = [
        {"day": "2026-09-10", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1000"},
        {"day": "2026-09-11", "open": "11", "high": "13", "low": "10", "close": "12", "volume": "1100"},
    ]
    rows = normalize_sina_payload(payload, "600519.SH", {"20260911"})
    assert rows[0]["volume_unit"] == "SHARE"
    assert rows[0]["pre_close"] == 11.0
    assert rows[0]["execution_eligible"] is False
    assert rows[0]["research_authorized"] is False
    assert rows[0]["amount"] is None


def test_tencent_batch_quote_requires_target_session_and_keeps_amount_unknown() -> None:
    fields = [""] * 37
    fields[3:6] = ["12", "11", "11.5"]
    fields[30] = "20260911150000"
    fields[33:35] = ["13", "10"]
    fields[36] = "101"
    payload = 'v_sh600519="' + "~".join(fields) + '";\n'
    rows = normalize_tencent_quote_payload(payload, "20260911")["600519.SH"]
    assert rows[0]["trade_date"] == "20260911"
    assert rows[0]["close"] == 12.0
    assert rows[0]["pre_close"] == 11.0
    assert rows[0]["volume_unit"] == "LOT_100_SHARES"
    assert rows[0]["amount"] is None
    assert normalize_tencent_quote_payload(payload, "20260910") == {}


def test_hashless_publication_helper_is_retired() -> None:
    result = update_daily_publication()
    assert result["status"] == "RETIRED_UNSAFE_ENTRYPOINT"
    assert result["publication_updated"] is False
