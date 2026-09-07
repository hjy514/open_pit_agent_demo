#!/usr/bin/env python3
"""P5: batch-calibrate directed planner reachability on CARLA map 0325_5.

The command is resumable by default.  It writes compact route facts to
``map_resources.db`` and does not spawn or drive any vehicle.
"""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore, calibrate_planner_reachability


def build_carla_0910_planner(carla_map, sampling_resolution):
    """Construct the DAO-based planner shipped with CARLA 0.9.10."""
    from agents.navigation.global_route_planner import GlobalRoutePlanner
    try:
        from agents.navigation.global_route_planner_dao import GlobalRoutePlannerDAO
    except ImportError:
        # Compatibility with later CARLA layouts; the project runtime remains
        # pinned to 0.9.10, but keeping this branch makes the failure clearer.
        return GlobalRoutePlanner(carla_map, sampling_resolution)
    planner = GlobalRoutePlanner(GlobalRoutePlannerDAO(carla_map, sampling_resolution))
    planner.setup()
    return planner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path,
        default=PROJECT_ROOT / "data" / "database" / "map_resources.db",
    )
    parser.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "configs" / "mine_competition_demo.json",
    )
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="0325_5")
    parser.add_argument("--expected-carla-version", default="0.9.10")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--sampling-resolution", type=float, default=2.0)
    parser.add_argument("--endpoint-tolerance-m", type=float, default=5.0)
    parser.add_argument(
        "--near-endpoint-tolerance-m", type=float, default=15.0,
        help="5米以上到此值的路线写入待单车复核状态，不进入自主派单",
    )
    parser.add_argument(
        "--pair-limit", type=int, default=250,
        help="本次最多处理的有向点对；默认250，重复运行自动继续",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="本次处理所有尚未完成的点对（覆盖--pair-limit）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="重新计算包括已完成结果在内的点对；一般不要使用",
    )
    parser.add_argument("--maximum-consecutive-errors", type=int, default=3)
    parser.add_argument(
        "--route-profile", type=Path,
        help="仅验证指定关键路线 JSON；与全图顺序批处理互不冲突。",
    )
    parser.add_argument(
        "--from-spawn-point-index", type=int,
        help="仅扫描一个起点到其余P3验证出生点的有向路线。",
    )
    parser.add_argument(
        "--exclude-target-spawn-point-index", type=int, action="append", default=[],
        help="定向扫描时排除目标出生点；可重复使用。",
    )
    args = parser.parse_args()
    if args.sampling_resolution <= 0:
        parser.error("--sampling-resolution must be positive")
    if args.pair_limit <= 0:
        parser.error("--pair-limit must be positive")
    if args.near_endpoint_tolerance_m < args.endpoint_tolerance_m:
        parser.error("--near-endpoint-tolerance-m must be >= --endpoint-tolerance-m")
    if args.route_profile and args.from_spawn_point_index is not None:
        parser.error("--route-profile 与 --from-spawn-point-index 不能同时使用")

    spawn_index_pairs = None
    profile_id = None
    if args.route_profile:
        try:
            profile = json.loads(args.route_profile.read_text(encoding="utf-8"))
            profile_id = str(profile["profile_id"])
            profile_map_id = str(profile["map_id"])
            route_items = list(profile["routes"])
            spawn_index_pairs = [
                (int(item["from_spawn_point_index"]), int(item["to_spawn_point_index"]))
                for item in route_items
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error("无效的 --route-profile：{}".format(exc))
        if not spawn_index_pairs:
            parser.error("--route-profile 的 routes 不能为空")
        if profile_map_id != args.map_id:
            parser.error(
                "路线配置地图 {} 与 --map-id {} 不一致".format(
                    profile_map_id, args.map_id
                )
            )
        print("P5关键路线配置：{}（{} 条有向路线）".format(
            profile_id, len(spawn_index_pairs)))

    try:
        adapter = CarlaAdapter(load_config(args.config))
        adapter._import_carla()
    except CarlaAdapterError as exc:
        raise RuntimeError(
            "无法加载CARLA Python API；请检查OPENPIT_CARLA_ROOT或--config"
        ) from exc

    client = adapter.carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    server_version = client.get_server_version()
    if args.expected_carla_version and args.expected_carla_version not in server_version:
        raise RuntimeError(
            "CARLA version mismatch: expected {}, got {}".format(
                args.expected_carla_version, server_version
            )
        )
    world = client.get_world()
    carla_map = world.get_map()
    planner = build_carla_0910_planner(carla_map, args.sampling_resolution)
    planner_version = "CARLA_GlobalRoutePlanner_0.9.10_res_{:.3f}m".format(
        args.sampling_resolution
    )

    def report_progress(processed, requested, record):
        if processed == 1 or processed == requested or processed % 25 == 0:
            print(
                "P5 progress: {}/{}; {} -> {}; {}".format(
                    processed, requested, record["from_point_id"],
                    record["to_point_id"], record["validation_status"],
                )
            )

    with MapResourceStore(args.database) as store:
        if args.from_spawn_point_index is not None:
            source_index = int(args.from_spawn_point_index)
            excluded = {source_index}.union(
                int(value) for value in args.exclude_target_spawn_point_index
            )
            verified_indices = [
                int(item["spawn_point_index"])
                for item in store.verified_spawn_points(args.map_id)
            ]
            if source_index not in verified_indices:
                raise RuntimeError(
                    "定向扫描起点{}不是P3 VERIFIED_SPAWN".format(source_index)
                )
            spawn_index_pairs = [
                (source_index, target_index)
                for target_index in verified_indices
                if target_index not in excluded
            ]
            print("P5定向扫描：{} -> {} 个候选目标；排除={}".format(
                source_index, len(spawn_index_pairs), sorted(excluded)))
        result = calibrate_planner_reachability(
            store=store,
            carla_map=carla_map,
            planner=planner,
            map_id=args.map_id,
            resource_version=args.resource_version,
            expected_map_name=args.expected_map_name,
            planner_version=planner_version,
            endpoint_tolerance_m=args.endpoint_tolerance_m,
            near_endpoint_tolerance_m=args.near_endpoint_tolerance_m,
            pair_limit=None if args.all else args.pair_limit,
            force=args.force,
            carla_server_version=server_version,
            maximum_consecutive_errors=args.maximum_consecutive_errors,
            progress_callback=report_progress,
            spawn_index_pairs=spawn_index_pairs,
        )

    print("P5 calibration run: {}".format(result["calibration_run_id"]))
    print("CARLA server version: {}; map: {}".format(server_version, carla_map.name))
    print(
        "本批：处理{pairs_processed}，可达{planner_reachable}，不可达{planner_unreachable}，"
        "待复核{planner_near_endpoint}，终点偏差{planner_endpoint_mismatch}，错误{planner_error}".format(**result)
    )
    print(
        "本次范围进度：{terminal_pairs_after_run}/{directed_pairs_total}；剩余{remaining_pairs}；完整={complete}".format(
            **result
        )
    )
    if profile_id:
        print("关键路线配置已完成：{}".format(profile_id))
    elif args.from_spawn_point_index is not None:
        print("定向扫描已完成：起点{}".format(args.from_spawn_point_index))
    else:
        print("全图点对总数：{}".format(result["global_directed_pairs_total"]))
    print(
        "Boundary: planner reachability only; physical driving, clearance, collision and fleet safety remain unverified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
