"""Print and persist a concise user-facing status snapshot."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantweb import v2_api  # noqa: E402
from src.quantcore.contracts.config import load_v2_config  # noqa: E402


def main() -> int:
    cfg = load_v2_config(create_dirs=True)
    overview = v2_api.overview()
    system = v2_api.system()
    payload = {
        "system_mode": overview.get("system_mode"),
        "full_scope_readiness": overview.get("full_scope_readiness"),
        "supported_scope_operational_readiness": overview.get("supported_scope_operational_readiness"),
        "status_cards": overview.get("status_cards"),
        "supported_profile": overview.get("supported_profile"),
        "runtime": system.get("runtime_status_counts"),
        "persisted_job_count": system.get("persisted_job_count"),
        "paper": v2_api.paper(),
        "watch": v2_api.watch(),
        "boundaries": overview.get("boundaries"),
    }
    target = cfg.paths.artifacts / "evidence" / "USER_DEMO" / "system_status.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8")
    os.replace(temporary, target)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
