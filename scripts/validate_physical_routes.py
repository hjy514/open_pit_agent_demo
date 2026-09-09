#!/usr/bin/env python3
"""P6: run isolated physical heavy-truck route attempts in CARLA."""

import argparse
from collections import Counter, defaultdict
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


def _edge_ids(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value.get("edge_ids", [])] \
        if isinstance(value, dict) else []


def select_complexity_candidates(candidates, limit, seed, blocked_pairs,
                                 existing_reached_pairs=None):
    """Select a reproducible mix of route diversity and event usefulness.

    This selects *calibration candidates*, not executable routes.  It keeps
    broad endpoint coverage while deliberately including limited shared
    destinations and topology overlap needed by takeover, congestion and
    road-control scenarios.  No candidate is promoted until CARLA records a
    PHYSICAL_REACHED result.
    """
    if limit < 1:
        return []
    items = [dict(item) for item in candidates]
    randomizer = Random(int(seed))
    randomizer.shuffle(items)
    tie_order = {
        (str(item["from_point_id"]), str(item["to_point_id"])): index
        for index, item in enumerate(items)
    }
    destination_frequency = Counter(
        str(item["to_point_id"]) for item in items
    )
    edge_frequency = Counter(
        edge_id for item in items for edge_id in item.get("edge_ids", [])[1:-1]
    )
    existing = {
        (str(a), str(b)) for a, b in (existing_reached_pairs or set())
    }
    reached_origins = {pair[0] for pair in existing}
    reached_destinations = {pair[1] for pair in existing}

    blocked = {
        tuple(sorted((str(a), str(b)))) for a, b in blocked_pairs
    }
    selected = []
    origin_use = Counter()
    destination_use = Counter()

    def allowed(item):
        origin = str(item["from_point_id"])
        destination = str(item["to_point_id"])
        if origin == destination or origin_use[origin] >= 2 \
                or destination_use[destination] >= 2:
            return False
        selected_origins = {
            str(existing_item["from_point_id"])
            for existing_item in selected
        }
        return not any(
            tuple(sorted((origin, other))) in blocked
            for other in selected_origins
        )

    def marginal_score(item):
        origin = str(item["from_point_id"])
        destination = str(item["to_point_id"])
        internal = set(item.get("edge_ids", [])[1:-1])
        selected_edges = {
            edge_id for existing_item in selected
            for edge_id in existing_item.get("edge_ids", [])[1:-1]
        }
        shared_with_pool = sum(
            max(0, edge_frequency[edge_id] - 1) for edge_id in internal
        )
        return (
            1000 if not origin_use[origin] else 0,
            800 if not destination_use[destination] else 0,
            500 if destination_frequency[destination] > 1 else 0,
            450 if internal.intersection(selected_edges) else 0,
            300 if origin not in reached_origins else 0,
            300 if destination not in reached_destinations else 0,
            250 if item.get("semantic_endpoint") else 0,
            min(200, shared_with_pool),
            min(100, int(item.get("junction_count") or 0) * 10),
            -tie_order[(origin, destination)],
        )

    def add(item):
        if item in selected or not allowed(item) or len(selected) >= int(limit):
            return False
        selected.append(item)
        origin_use[str(item["from_point_id"])] += 1
        destination_use[str(item["to_point_id"])] += 1
        return True

    # Reserve a small, bounded amount of route-pool capacity for two
    # complexity primitives. They are still isolated-route candidates:
    # simultaneous traffic safety is verified later at runtime.
    destination_groups = defaultdict(list)
    for item in items:
        destination_groups[str(item["to_point_id"])].append(item)
    shared_destination_groups = [
        group for group in destination_groups.values()
        if len({str(item["from_point_id"]) for item in group}) >= 2
    ]
    shared_destination_groups.sort(key=lambda group: (
        any(item.get("semantic_endpoint") for item in group),
        len(group),
        max(int(item.get("junction_count") or 0) for item in group),
    ), reverse=True)
    for group in shared_destination_groups:
        before = len(selected)
        for item in sorted(group, key=marginal_score, reverse=True):
            add(item)
            if len(selected) - before == 2:
                break
        if len(selected) - before == 2:
            break

    edge_groups = defaultdict(list)
    for item in items:
        for edge_id in set(item.get("edge_ids", [])[1:-1]):
            edge_groups[edge_id].append(item)
    shared_edge_groups = [group for group in edge_groups.values()
                          if len(group) >= 2]
    shared_edge_groups.sort(key=lambda group: (
        len(group),
        any(item.get("semantic_endpoint") for item in group),
    ), reverse=True)
    for group in shared_edge_groups:
        before = len(selected)
        for item in sorted(group, key=marginal_score, reverse=True):
            add(item)
            if len(selected) - before == 2:
                break
        if len(selected) - before == 2:
            break

    while len(selected) < int(limit):
        available = [item for item in items if item not in selected and allowed(item)]
        if not available:
            break
        chosen = max(available, key=marginal_score)
        add(chosen)
    return selected


def candidate_selection_summary(routes):
    origins = [str(item["from_point_id"]) for item in routes]
    destinations = [str(item["to_point_id"]) for item in routes]
    edge_usage = Counter(
        edge_id for item in routes for edge_id in item.get("edge_ids", [])[1:-1]
    )
    return {
        "route_count": len(routes),
        "unique_origin_count": len(set(origins)),
        "unique_destination_count": len(set(destinations)),
        "shared_destination_route_count": sum(
            count for count in Counter(destinations).values() if count > 1
        ),
        "topology_overlap_edge_count": sum(
            count > 1 for count in edge_usage.values()
        ),
        "semantic_endpoint_route_count": sum(
            bool(item.get("semantic_endpoint")) for item in routes
        ),
        "claim": "P6_CANDIDATES_ONLY_PENDING_CARLA_VALIDATION",
    }


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
    parser.add_argument(
        "--candidate-strategy", choices=("complexity", "independent"),
        default="complexity",
        help=(
            "自动候选策略：complexity兼顾作业区、端点覆盖、共享终点"
            "和路网重叠；independent保留原独立起终点选择。"
        ),
    )
    parser.add_argument("--from-spawn-point-index", type=int,
                        help="仅从指定CARLA出生点选择自动P6候选；需与--auto-candidates一起使用。")
    parser.add_argument("--exclude-target-spawn-point-index", type=int, action="append", default=[],
                        help="自动候选时排除指定目标点；可重复使用。")
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
    if args.from_spawn_point_index is not None and not args.auto_candidates:
        parser.error("--from-spawn-point-index需要与--auto-candidates一起使用")
    if args.minimum_route_length_m < 0 or args.maximum_route_length_m < args.minimum_route_length_m:
        parser.error("候选路线长度范围无效")
    if args.auto_candidates:
        with MapResourceStore(args.database) as candidate_store:
            physical_records = list(candidate_store.physical_route_validations(
                args.map_id, args.resource_version
            ))
            attempted = {
                (str(item["from_point_id"]), str(item["to_point_id"]))
                for item in physical_records
            }
            reached = {
                (str(item["from_point_id"]), str(item["to_point_id"]))
                for item in physical_records
                if item.get("validation_status") == "PHYSICAL_REACHED"
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
            route_rows = candidate_store.connection.execute(
                "SELECT from_point_id,to_point_id,road_lane_sequence_json "
                "FROM route_candidates WHERE map_id=? AND resource_version=?",
                (args.map_id, args.resource_version),
            ).fetchall()
            route_edges = {
                (str(row[0]), str(row[1])): _edge_ids(row[2])
                for row in route_rows
            }
            semantic_points = {
                str(point_id)
                for area in candidate_store.operating_areas(
                    args.map_id, args.resource_version
                )
                if area.get("selection_mode") != "not_selectable"
                for point_id in area.get("point_ids", [])
            }
            for item in candidates:
                pair = (str(item["from_point_id"]), str(item["to_point_id"]))
                item["edge_ids"] = route_edges.get(pair, [])
                item["semantic_endpoint"] = (
                    pair[0] in semantic_points or pair[1] in semantic_points
                )
        if args.from_spawn_point_index is not None:
            requested_origin = "carla-spawn:{}".format(args.from_spawn_point_index)
            candidates = [item for item in candidates
                          if str(item["from_point_id"]) == requested_origin]
        excluded_targets = {"carla-spawn:{}".format(index)
                            for index in args.exclude_target_spawn_point_index}
        if excluded_targets:
            candidates = [item for item in candidates
                          if str(item["to_point_id"]) not in excluded_targets]
        if args.candidate_strategy == "complexity" \
                and args.from_spawn_point_index is None:
            selected_candidates = select_complexity_candidates(
                candidates, args.auto_candidates, args.seed, blocked,
                existing_reached_pairs=reached,
            )
        else:
            Random(args.seed).shuffle(candidates)
            selected_candidates, origins, destinations = [], set(), set()
            for item in candidates:
                origin = str(item["from_point_id"])
                destination = str(item["to_point_id"])
                if origin in origins or destination in destinations:
                    continue
                if origin in destinations or destination in origins:
                    continue
                if any(tuple(sorted((origin, other))) in blocked
                       for other in origins):
                    continue
                selected_candidates.append(item)
                origins.add(origin)
                destinations.add(destination)
                if len(selected_candidates) == args.auto_candidates:
                    break
        selection_summary = candidate_selection_summary(selected_candidates)
        routes = []
        for item in selected_candidates:
            origin = str(item["from_point_id"])
            destination = str(item["to_point_id"])
            routes.append({
                "route_id": "auto-p6-{}-{}".format(
                    origin.rsplit(":", 1)[-1], destination.rsplit(":", 1)[-1]
                ),
                "from_spawn_point_index": int(origin.rsplit(":", 1)[-1]),
                "to_spawn_point_index": int(destination.rsplit(":", 1)[-1]),
                "purpose": (
                    "复杂场景路线池P6单矿卡扩展验证"
                    if args.candidate_strategy == "complexity"
                    else "P5严格可达点对的P6单矿卡扩展验证"
                ),
                "from_point_id": origin,
                "to_point_id": destination,
                "semantic_endpoint": bool(item.get("semantic_endpoint")),
            })
        if len(routes) < args.auto_candidates:
            parser.error("符合路线选择和P4约束的新候选不足：{}/{}".format(
                len(routes), args.auto_candidates
            ))
        profile_id = "auto-p6-seed-{}-{}routes".format(args.seed, len(routes))
        print("P6自动候选：{}；策略={}；路线数={}；长度范围={:.0f}-{:.0f}m；起点限制={}；排除目标={}".format(
            profile_id, args.candidate_strategy, len(routes), args.minimum_route_length_m,
            args.maximum_route_length_m,
            args.from_spawn_point_index if args.from_spawn_point_index is not None else "无",
            sorted(args.exclude_target_spawn_point_index),
        ))
    else:
        try:
            profile = json.loads(args.route_profile.read_text(encoding="utf-8"))
            profile_id = str(profile["profile_id"])
            if str(profile["map_id"]) != args.map_id:
                raise ValueError("profile map_id 与 --map-id 不一致")
            routes = list(profile["routes"])
            selection_summary = None
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
            "candidate_strategy": (
                args.candidate_strategy if args.auto_candidates else "profile"
            ),
            "route_count": len(routes), "selection_summary": selection_summary,
            "routes": routes,
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
