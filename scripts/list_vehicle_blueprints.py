#!/usr/bin/env python3
"""List vehicle blueprints exposed by the running CARLA server."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "mine_competition_demo.json"
TRUCK_KEYWORDS = (
    "mine",
    "mining",
    "truck",
    "dump",
    "haul",
    "articulated",
    "daf",
    "carlacola",
    "firetruck",
    # Workspace-specific cooked mine-truck blueprint names.
    "cat_ceshi",
    "cat_xin",
    "jiaojie",
)
CUSTOM_TRUCK_BLUEPRINT_IDS = (
    "vehicle.cat.cat",
    "vehicle.cam.cam",
    "vehicle.190.190",
)


def main():
    parser = argparse.ArgumentParser(
        description="查询当前CARLA实例可生成的车辆蓝图"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--all",
        action="store_true",
        help="显示所有vehicle.*蓝图，而不只是卡车候选项",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    adapter = CarlaAdapter(config)
    adapter._import_carla()

    client = adapter.carla.Client(config.carla.host, config.carla.port)
    client.set_timeout(config.carla.timeout_seconds)
    world = client.get_world()
    map_name = world.get_map().name.split("/")[-1]

    blueprint_ids = sorted(
        blueprint.id
        for blueprint in world.get_blueprint_library().filter("vehicle.*")
    )
    candidates = [
        blueprint_id
        for blueprint_id in blueprint_ids
        if (
            blueprint_id in CUSTOM_TRUCK_BLUEPRINT_IDS
            or any(
                keyword in blueprint_id.lower()
                for keyword in TRUCK_KEYWORDS
            )
        )
    ]

    print("CARLA地图：{}".format(map_name))
    print("可用车辆蓝图总数：{}".format(len(blueprint_ids)))
    print("矿车/卡车候选蓝图：{}".format(len(candidates)))
    if candidates:
        for blueprint_id in candidates:
            print("- {}".format(blueprint_id))
    else:
        print("- 未发现名称中含矿车或卡车关键字的可生成蓝图")

    if args.all:
        print("全部车辆蓝图：")
        for blueprint_id in blueprint_ids:
            print("- {}".format(blueprint_id))


if __name__ == "__main__":
    main()
