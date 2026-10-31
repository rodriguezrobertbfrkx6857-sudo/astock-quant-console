from __future__ import annotations

from quantweb.v2_api import _apply_factor_lifecycle


def test_factor_inventory_marks_only_latest_version_executable() -> None:
    items = [
        {
            "factor_id": "example.v1",
            "factor_version_id": "example.v1",
            "base_factor_id": "example",
            "version": "v1",
        },
        {
            "factor_id": "example.v2",
            "factor_version_id": "example.v2",
            "base_factor_id": "example",
            "version": "v2",
        },
    ]

    resolved = _apply_factor_lifecycle(items)

    assert not resolved[0]["executable"]
    assert resolved[0]["lifecycle"] == "SUPERSEDED_AUDIT_ONLY"
    assert resolved[1]["executable"]
    assert resolved[1]["lifecycle"] == "CURRENT"


def test_current_factor_with_missing_dependency_is_not_executable() -> None:
    resolved = _apply_factor_lifecycle(
        [
            {
                "factor_id": "example.v2",
                "factor_version_id": "example.v2",
                "base_factor_id": "example",
                "version": "v2",
                "dependency_ready": False,
            }
        ]
    )

    assert resolved[0]["lifecycle"] == "CURRENT"
    assert not resolved[0]["executable"]
