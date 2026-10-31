from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.quantcore.contracts.config import load_v2_config  # noqa:E402
from src.quantcore.contracts.safety import validate_runtime_safety  # noqa:E402


def main() -> int:
    cfg = load_v2_config(create_dirs=True)
    validate_runtime_safety(host=cfg.server.host)
    import uvicorn

    uvicorn.run(
        "quantweb.app:app", host=cfg.server.host, port=cfg.server.port, log_level="info"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
