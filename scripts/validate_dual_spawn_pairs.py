#!/usr/bin/env python3
"""P4 validate the closest static conflict candidates with two CARLA mine trucks."""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.dual_spawn_calibrator import select_static_candidates, verify_dual_spawn_pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "mine_competition_demo.json")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="0325_5")
    parser.add_argument("--expected-carla-version", default="0.9.10")
    parser.add_argument("--pair-limit", type=int, default=10)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.pair_limit <= 0:
        parser.error("--pair-limit must be positive")
    try:
        adapter = CarlaAdapter(load_config(args.config)); adapter._import_carla(); carla = adapter.carla
    except CarlaAdapterError as exc:
        raise RuntimeError("无法加载 CARLA Python API；请检查 OPENPIT_CARLA_ROOT 或 --config") from exc
    client = carla.Client(args.host, args.port); client.set_timeout(args.timeout)
    server_version = client.get_server_version(); world = client.get_world()
    if args.expected_carla_version and args.expected_carla_version not in server_version:
        raise RuntimeError("CARLA version mismatch: expected {}, got {}".format(
            args.expected_carla_version, server_version))
    spawn_points = world.get_map().get_spawn_points()
    def transform_factory(point):
        index = point.get("spawn_point_index")
        if index is None or not 0 <= int(index) < len(spawn_points):
            raise RuntimeError("数据库 Spawn Point 索引超出当前 CARLA 地图范围: {}".format(index))
        return spawn_points[int(index)]
    with MapResourceStore(args.database) as store:
        pairs = select_static_candidates(store, args.map_id, args.resource_version, args.pair_limit)
        if not pairs:
            raise RuntimeError("没有可验证的 STATIC_INFERRED 点对；请先运行 P4 静态推断")
        result = verify_dual_spawn_pairs(store, world, transform_factory, args.map_id, args.resource_version, args.expected_map_name, pairs, carla_server_version=server_version)
    print("P4 dual spawn run: {calibration_run_id}; requested={pairs_requested}; verified={dual_spawn_verified}; blocked={dual_spawn_blocked}".format(**result))
    print("Boundary: simultaneous Spawn only; driving, collision, clearance and multi-vehicle capacity remain unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
