from __future__ import annotations

from quantweb import v2_api


def test_data_sample_prefers_canonical_market_rows(monkeypatch) -> None:
    rows = [
        {"ts_code": "510300.SH", "trade_date": "20260909", "close": 4.637},
        {"ts_code": "510300.SH", "trade_date": "20260910", "close": 4.617},
    ]
    monkeypatch.setattr(
        v2_api,
        "market_daily",
        lambda **_: {
            "dataset": "etf3_current_prices",
            "datasets_used": ["etf3_current_prices"],
            "manifest_hashes": {"etf3_current_prices": "abc"},
            "rows": rows,
            "provenance": "REAL",
            "state": "REAL_READY_CURRENT_ETF_SCOPE",
            "price_basis": "UNADJUSTED_EXECUTION_PRICE",
        },
    )

    result = v2_api.data_sample(
        dataset="", ts_code="510300.SH", start_date="", end_date="", limit=1
    )

    assert result["state"] == "REAL_READY_CURRENT_ETF_SCOPE"
    assert result["source_kind"] == "V2_CANONICAL"
    assert result["sample"] == rows[-1:]
