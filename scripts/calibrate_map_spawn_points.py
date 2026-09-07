#!/usr/bin/env python3
"""P3: calibrate every 0325_5 spawn point with one temporary heavy truck.

Requires a running CARLA 0.9.10 server already loaded with the requested map.
It writes only ``map_resources.db``.  A PASS means actor generation plus a
basic Driving-lane check, not route, clearance, collision, or fleet safety.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.carla_calibrator import calibrate_spawn_points
from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "mine_competition_demo.json",
        help="用于复用 CARLA Python API 路径配置的现有场景配置",
    )
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="0325_5")
    parser.add_argument("--vehicle-blueprint", default="vehicle.cat.cat")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--expected-carla-version", default="0.9.10")
    parser.add_argument("--maximum-z-offset-m", type=float, default=2.0,
                        help="Spawn 与 CARLA waypoint 的最大允许高程差")
    args = parser.parse_args()
    # Reuse the established adapter bootstrap.  It finds the CARLA 0.9.10
    # Python egg and agents package from config.carla.root / OPENPIT_CARLA_ROOT,
    # so this standalone calibration tool behaves like run_demo.py.
    try:
        adapter = CarlaAdapter(load_config(args.config))
        adapter._import_carla()
        carla = adapter.carla
    except CarlaAdapterError as exc:
        raise RuntimeError(
            "无法加载 CARLA Python API；请检查 OPENPIT_CARLA_ROOT 或 --config"
        ) from exc
    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    server_version = client.get_server_version()
    if args.expected_carla_version and args.expected_carla_version not in server_version:
        raise RuntimeError("CARLA server version mismatch: expected {}, got {}".format(
            args.expected_carla_version, server_version))
    world = client.get_world()
    with MapResourceStore(args.database) as store:
        result = calibrate_spawn_points(
            store, world, args.map_id, args.resource_version, args.expected_map_name,
            vehicle_blueprint=args.vehicle_blueprint,
            maximum_z_offset_m=args.maximum_z_offset_m,
            carla_server_version=server_version,
        )
    print("P3 calibration run: {}".format(result["calibration_run_id"]))
    print("CARLA server version: {}; map: {}".format(server_version, world.get_map().name))
    print("Spawn points: {spawn_points}; VERIFIED_SPAWN: {verified_spawn}; EXCLUDED: {excluded}".format(**result))
    print("Boundary: single-spawn/basic-lane verification only; routes, clearance, collisions and multi-vehicle safety remain unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
