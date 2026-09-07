#!/usr/bin/env python3
"""P6: run isolated physical heavy-truck route attempts in CARLA."""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from random import Random

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore, drive_single_route


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "mine_competition_demo.json")
    parser.add_argument("--route-profile", type=Path, default=PROJECT_ROOT / "configs" / "map_resource_slope_demo_physical_routes.json")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="0325_5")
    parser.add_argument("--expected-carla-version", default="0.9.10")
    parser.add_argument("--target-speed-kmh", type=float, default=15.0)
    parser.add_argument("--arrival-tolerance-m", type=float, default=12.0)
    parser.add_argument("--max-duration-seconds", type=float, default=360.0)
    parser.add_argument("--stuck-window-seconds", type=float, default=25.0)
    parser.add_argument("--minimum-progress-m", type=float, default=3.0)
    parser.add_argument("--route-id", action="append", help="仅验证指定route_id；可重复使用。")
    parser.add_argument("--auto-candidates", type=int, default=0,
                        help="从P5严格可达点对中可复现地选择N条新P6候选。")
    parser.add_argument("--seed", type=int, default=202616)
    parser.add_argument("--minimum-route-length-m", type=float, default=100.0)
    parser.add_argument("--maximum-route-length-m", type=float, default=1000.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="只输出本次路线集，不连接CARLA、不写数据库。")
    args = parser.parse_args()
    if args.target_speed_kmh <= 0 or args.arrival_tolerance_m <= 0 or args.max_duration_seconds <= 0:
        parser.error("速度、到达容差和最大时长必须大于0")

    if args.auto_candidates < 0:
        parser.error("--auto-candidates不能为负数")
    if args.minimum_route_length_m < 0 or args.maximum_route_length_m < args.minimum_route_length_m:
        parser.error("候选路线长度范围无效")
    if args.auto_candidates:
        with MapResourceStore(args.database) as candidate_store:
            attempted = {
                (str(item["from_point_id"]), str(item["to_point_id"]))
                for item in candidate_store.physical_route_validations(
                    args.map_id, args.resource_version
                )
            }
            blocked = {
                tuple(sorted((str(a), str(b))))
                for a, b in candidate_store.blocked_dual_spawn_pairs(
                    args.map_id, args.resource_version
                )
            }
            candidates = [
                dict(item) for item in candidate_store.planner_reachable_pairs(
                    args.map_id, args.resource_version
                )
                if args.minimum_route_length_m <= float(item.get("route_length_m") or 0.0)
                <= args.maximum_route_length_m
                and (str(item["from_point_id"]), str(item["to_point_id"])) not in attempted
            ]
        Random(args.seed).shuffle(candidates)
        routes, origins, destinations = [], set(), set()
        for item in candidates:
            origin, destination = str(item["from_point_id"]), str(item["to_point_id"])
            if origin in origins or destination in destinations:
                continue
            if origin in destinations or destination in origins:
                continue
            if any(tuple(sorted((origin, other))) in blocked for other in origins):
                continue
            routes.append({
                "route_id": "auto-p6-{}-{}".format(
                    origin.rsplit(":", 1)[-1], destination.rsplit(":", 1)[-1]
                ),
                "from_spawn_point_index": int(origin.rsplit(":", 1)[-1]),
                "to_spawn_point_index": int(destination.rsplit(":", 1)[-1]),
                "purpose": "P5严格可达点对的P6单矿卡扩展验证",
            })
            origins.add(origin)
            destinations.add(destination)
            if len(routes) == args.auto_candidates:
                break
        if len(routes) < args.auto_candidates:
            parser.error("符合独立起终点和P4约束的新候选不足：{}/{}".format(
                len(routes), args.auto_candidates
            ))
        profile_id = "auto-p6-seed-{}-{}routes".format(args.seed, len(routes))
        print("P6自动候选：{}；路线数={}；长度范围={:.0f}-{:.0f}m".format(
            profile_id, len(routes), args.minimum_route_length_m,
            args.maximum_route_length_m,
        ))
    else:
        try:
            profile = json.loads(args.route_profile.read_text(encoding="utf-8"))
            profile_id = str(profile["profile_id"])
            if str(profile["map_id"]) != args.map_id:
                raise ValueError("profile map_id 与 --map-id 不一致")
            routes = list(profile["routes"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            parser.error("无效路线配置：{}".format(exc))
    if args.route_id:
        route_ids = set(args.route_id)
        routes = [item for item in routes if item.get("route_id") in route_ids]
        missing = route_ids.difference({item.get("route_id") for item in routes})
        if missing:
            parser.error("路线配置中不存在：{}".format(", ".join(sorted(missing))))
    if not routes:
        parser.error("没有待验证路线")
    if args.dry_run:
        print(json.dumps({
            "status": "DRY_RUN", "profile_id": profile_id,
            "route_count": len(routes), "routes": routes,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    adapter = CarlaAdapter(load_config(args.config))
    try:
        adapter._import_carla()
    except CarlaAdapterError as exc:
        raise RuntimeError("无法加载 CARLA Python API") from exc
    carla = adapter.carla
    client = carla.Client(adapter.config.carla.host, adapter.config.carla.port)
    client.set_timeout(adapter.config.carla.timeout_seconds)
    server_version = client.get_server_version()
    if args.expected_carla_version not in server_version:
        raise RuntimeError("CARLA版本不匹配：{}".format(server_version))
    world = client.get_world()
    carla_map = world.get_map()
    current_name = carla_map.name.split("/")[-1]
    if current_name != args.expected_map_name:
        raise RuntimeError("当前地图是{}，期望{}；本工具不会自动切图".format(current_name, args.expected_map_name))
    spawn_points = carla_map.get_spawn_points()
    blueprint = world.get_blueprint_library().find("vehicle.cat.cat")
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", "p6_physical_route_validation")

    run_id = "physical-route-validation-{}".format(uuid.uuid4().hex)
    summary = {"attempted": 0, "by_status": {}, "profile_id": profile_id, "server_version": server_version}
    with MapResourceStore(args.database) as store:
        verified = {int(item["spawn_point_index"]): item for item in store.verified_spawn_points(args.map_id)}
        store.start_calibration_run(
            run_id, args.map_id, args.resource_version, "CARLA_PHYSICAL_ROUTE_VALIDATION",
            "phase6-v1", "P6_SINGLE_HEAVY_TRUCK_BASIC_AGENT",
            "Isolated default-physics vehicle.cat.cat attempts; actors are destroyed after every route.",
        )
        try:
            for index, route in enumerate(routes, start=1):
                source_index = int(route["from_spawn_point_index"])
                target_index = int(route["to_spawn_point_index"])
                if source_index not in verified or target_index not in verified:
                    raise RuntimeError("P6路线引用非P3验证点：{} -> {}".format(source_index, target_index))
                if source_index >= len(spawn_points) or target_index >= len(spawn_points):
                    raise RuntimeError("P6路线Spawn Point索引超出当前地图范围")
                started_at = now_iso()
                result = drive_single_route(
                    carla, world, adapter._basic_agent_class, blueprint,
                    spawn_points[source_index], spawn_points[target_index].location,
                    target_speed_kmh=args.target_speed_kmh,
                    arrival_tolerance_m=args.arrival_tolerance_m,
                    max_duration_seconds=args.max_duration_seconds,
                    stuck_window_seconds=args.stuck_window_seconds,
                    minimum_progress_m=args.minimum_progress_m,
                )
                store.add_route_execution_validation({
                    "validation_id": "physical-route-attempt-{}".format(uuid.uuid4().hex),
                    "calibration_run_id": run_id, "map_id": args.map_id,
                    "resource_version": args.resource_version, "route_profile_id": profile_id,
                    "route_id": str(route["route_id"]),
                    "from_point_id": verified[source_index]["point_id"],
                    "to_point_id": verified[target_index]["point_id"],
                    "vehicle_blueprint": "vehicle.cat.cat", "target_speed_kmh": args.target_speed_kmh,
                    "arrival_tolerance_m": args.arrival_tolerance_m,
                    "validation_status": result.status, "started_at": started_at,
                    "ended_at": now_iso(), "duration_seconds": result.duration_seconds,
                    "tick_count": result.tick_count, "initial_distance_m": result.initial_distance_m,
                    "final_distance_m": result.final_distance_m,
                    "distance_travelled_m": result.distance_travelled_m, "notes": result.notes,
                })
                summary["attempted"] += 1
                summary["by_status"][result.status] = summary["by_status"].get(result.status, 0) + 1
                print("P6 {}/{}: {} {} -> {} : {}; final={:.2f}m; {:.1f}s".format(
                    index, len(routes), route["route_id"], source_index, target_index,
                    result.status, result.final_distance_m if result.final_distance_m is not None else -1.0,
                    result.duration_seconds))
            store.finish_calibration_run(run_id, "PASS", summary)
        except BaseException as exc:
            summary["error"] = "{}: {}".format(type(exc).__name__, exc)
            store.finish_calibration_run(run_id, "FAILED", summary)
            raise
    print("P6 validation run: {}".format(run_id))
    print("Summary: {}".format(json.dumps(summary, ensure_ascii=False, sort_keys=True)))
    print("Boundary: isolated single-truck traversal only; no multi-vehicle collision, clearance or traffic validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
