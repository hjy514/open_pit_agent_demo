#!/usr/bin/env python3
"""Print the read-only map-resource admission report for one scenario."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import admit_scenario_resources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "configs" / "s01_normal_6v.json")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--mode", choices=("structural_mock", "carla_validation"),
                        default="structural_mock")
    args = parser.parse_args()
    report = admit_scenario_resources(
        load_config(args.config), seed=args.seed, execution_mode=args.mode
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
