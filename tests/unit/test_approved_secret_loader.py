from __future__ import annotations

import os

from src.quantcore.contracts.secrets import (
    ensure_relay_credential_from_approved_project_files,
)


def test_existing_process_credential_is_not_replaced(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_RELAY_KEY", "tsr_test_sentinel")

    status = ensure_relay_credential_from_approved_project_files()

    assert status == "PROCESS_ENV"
    assert os.environ["TUSHARE_RELAY_KEY"] == "tsr_test_sentinel"
