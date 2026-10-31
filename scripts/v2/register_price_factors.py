from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.quantcore.contracts.config import load_v2_config  # noqa: E402
from src.quantcore.factors.evaluator import evaluate_price_factor, register_price_factors  # noqa: E402
from src.quantcore.factors.price import FACTOR_CATALOG  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Register and evaluate real bounded ETF price factors")
    parser.add_argument("--evaluate", action="store_true", help="evaluate all registered factors")
    args = parser.parse_args()
    cfg = load_v2_config(create_dirs=True)
    report: dict[str, object] = {"registration": register_price_factors(cfg)}
    if args.evaluate:
        report["evaluations"] = [
            evaluate_price_factor(cfg, factor_version_id)
            for factor_version_id, definition in FACTOR_CATALOG.items()
            if definition.family_id
            not in {"TURNOVER_RATE", "BETA", "IDIOSYNCRATIC_VOLATILITY"}
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
