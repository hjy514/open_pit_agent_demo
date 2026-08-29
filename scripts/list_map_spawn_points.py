#!/usr/bin/env python3
"""Print real CARLA spawn points for mine-scenario spatial calibration."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Read 0325_5 spawn-point coordinates without changing CARLA"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "mine_competition_demo.json",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Print only the first N points; 0 prints all points",
    )
    args = parser.parse_args()

    adapter = CarlaAdapter(load_config(args.config))
    adapter.connect()
    try:
        carla_map = adapter.world.get_map()
        points = list(carla_map.get_spawn_points())
        limit = int(args.limit)
        selected = points if limit <= 0 else points[:limit]
        print("CARLA地图：{}".format(carla_map.name.split("/")[-1]))
        print("Spawn point总数：{}".format(len(points)))
        print("index,x,y,z,yaw")
        for index, transform in enumerate(selected):
            location = transform.location
            rotation = transform.rotation
            print(
                "{},{:.3f},{:.3f},{:.3f},{:.3f}".format(
                    index,
                    location.x,
                    location.y,
                    location.z,
                    rotation.yaw,
                )
            )
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
