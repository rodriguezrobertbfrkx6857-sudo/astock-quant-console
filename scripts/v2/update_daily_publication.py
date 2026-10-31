"""Retired unsafe publication helper.

The previous command created REAL_READY metadata without content hashes and
pointed ``daily`` at a mutable SQLite file. Publication must now be performed
by ``tencent_fallback_sync.py`` or ``sina_fallback_sync.py`` through the V2
content-addressed data contract.
"""

from __future__ import annotations

import json


def update_daily_publication() -> dict[str, object]:
    return {
        "status": "RETIRED_UNSAFE_ENTRYPOINT",
        "publication_updated": False,
        "replacement": "scripts/v2/tencent_fallback_sync.py",
        "reason": "hashless metadata and mutable Legacy SQLite are not publishable V2 evidence",
    }


if __name__ == "__main__":
    print(json.dumps(update_daily_publication(), ensure_ascii=False, indent=2))
    raise SystemExit(2)
