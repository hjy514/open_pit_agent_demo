#!/usr/bin/env python3
"""Create or validate the 0325_5 Mine Spatial Resource Library database.

Phase 1 intentionally writes metadata only.  It does not connect to CARLA
and it does not classify any map point as verified.
"""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path,
        default=PROJECT_ROOT / "data" / "database" / "map_resources.db",
    )
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--map-name", default="0325_5")
    parser.add_argument("--carla-map-name", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--vehicle-blueprint", default="vehicle.cat.cat")
    parser.add_argument("--xodr-path", type=Path)
    parser.add_argument("--carla-version", default="0.9.10")
    parser.add_argument("--planner-version", default="CARLA_GlobalRoutePlanner")
    args = parser.parse_args()

    with MapResourceStore(args.database) as store:
        store.initialise_map(
            map_id=args.map_id,
            map_name=args.map_name,
            carla_map_name=args.carla_map_name,
            resource_version=args.resource_version,
            vehicle_blueprint=args.vehicle_blueprint,
            xodr_path=args.xodr_path,
            carla_version=args.carla_version,
            planner_version=args.planner_version,
            notes="0325_5 Mine Spatial Resource Library V1 / Phase 1",
        )
        result = store.validate_schema()

    print("矿区空间资源库数据库：{}".format(result["database_path"]))
    print("Schema V{}，必需表：{}，外键：{}".format(
        result["schema_version"], result["required_tables"],
        "开启" if result["foreign_keys_enabled"] else "关闭",
    ))
    if result["missing_tables"]:
        print("缺失表：{}".format(", ".join(result["missing_tables"])))
        return 1
    print("验证：PASS（当前仅完成 Phase 1 元数据和数据库骨架）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
