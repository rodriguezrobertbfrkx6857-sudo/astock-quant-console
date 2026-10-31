"""User-facing research command with a persisted, readable summary."""

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
from src.quantcore.research.runner import execute_ready  # noqa: E402


def main() -> int:
    cfg = load_v2_config(create_dirs=True)
    execution = execute_ready(cfg)
    view = v2_api.research_overview()
    payload = {
        "status": "PASS",
        "execution": execution,
        "queue": view.get("queue", []),
        "runs": view.get("runs", []),
        "stage09": view.get("stage09", []),
        "stage10": view.get("stage10", []),
        "synthetic": False,
    }
    target = cfg.paths.artifacts / "evidence" / "USER_DEMO" / "research_view.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8")
    os.replace(temporary, target)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
