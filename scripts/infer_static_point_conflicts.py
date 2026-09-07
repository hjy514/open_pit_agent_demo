#!/usr/bin/env python3
"""P4 static point-conflict inference for existing P3 verified spawn points.

This is an offline distance filter.  Its threshold is a user-supplied policy
value, not a measured vehicle footprint or a CARLA collision verdict.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.point_conflicts import infer_static_point_conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path,
                        default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--minimum-center-distance-m", type=float, required=True,
                        help="保守策略阈值；并非矿卡实测尺寸")
    args = parser.parse_args()
    with MapResourceStore(args.database) as store:
        result = infer_static_point_conflicts(
            store, args.map_id, args.resource_version, args.minimum_center_distance_m
        )
    print("P4 static inference: verified points={verified_spawn_points}; pairs={evaluated_pairs}; candidate conflicts={candidate_conflicts}".format(**result))
    print("Boundary: STATIC_INFERRED only; two-vehicle CARLA validation is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
