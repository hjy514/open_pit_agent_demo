#!/usr/bin/env python3
"""Read-only route calibration helper for CARLA maps."""

import argparse
import math
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config


def route_length(route):
    total = 0.0
    previous = None
    for waypoint, _ in route:
        location = waypoint.transform.location
        if previous is not None:
            total += math.sqrt(
                (location.x - previous.x) ** 2
                + (location.y - previous.y) ** 2
                + (location.z - previous.z) ** 2
            )
        previous = location
    return total


def route_endpoint_error(route, target_location):
    if not route:
        return float("inf")
    endpoint = route[-1][0].transform.location
    return math.sqrt(
        (endpoint.x - target_location.x) ** 2
        + (endpoint.y - target_location.y) ** 2
        + (endpoint.z - target_location.z) ** 2
    )


def main():
    parser = argparse.ArgumentParser(
        description="Recommend short reachable target spawn points"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "town03.json",
    )
    parser.add_argument("--minimum-route-m", type=float, default=30.0)
    parser.add_argument("--maximum-route-m", type=float, default=80.0)
    parser.add_argument(
        "--maximum-endpoint-error-m",
        type=float,
        default=8.0,
        help="Reject routes whose final waypoint is farther from the target",
    )
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--origin-spawn-point-index",
        type=int,
        help=(
            "Override every configured vehicle origin; useful for "
            "checking a post-preemption route from an intermediate point"
        ),
    )
    parser.add_argument(
        "--common-vehicle-ids",
        nargs="+",
        help="Also recommend targets reachable by every listed vehicle",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    adapter = CarlaAdapter(config)
    adapter.connect()
    try:
        carla_map = adapter.world.get_map()
        spawn_points = carla_map.get_spawn_points()
        from agents.navigation.global_route_planner import GlobalRoutePlanner
        from agents.navigation.global_route_planner_dao import (
            GlobalRoutePlannerDAO,
        )

        planner = GlobalRoutePlanner(
            GlobalRoutePlannerDAO(carla_map, 2.0)
        )
        planner.setup()

        route_metrics = {}
        for vehicle in config.vehicles:
            configured_origin = (
                args.origin_spawn_point_index
                if args.origin_spawn_point_index is not None
                else vehicle.spawn_point_index
            )
            origin_index = configured_origin % len(spawn_points)
            origin = spawn_points[origin_index].location
            candidates = []
            route_metrics[vehicle.vehicle_id] = {}
            for target_index, transform in enumerate(spawn_points):
                if target_index == origin_index:
                    continue
                route = planner.trace_route(origin, transform.location)
                length = route_length(route)
                endpoint_error = route_endpoint_error(
                    route, transform.location
                )
                direct_distance = math.sqrt(
                    (transform.location.x - origin.x) ** 2
                    + (transform.location.y - origin.y) ** 2
                    + (transform.location.z - origin.z) ** 2
                )
                route_metrics[vehicle.vehicle_id][target_index] = (
                    length,
                    direct_distance,
                    endpoint_error,
                )
                if (
                    args.minimum_route_m
                    <= length
                    <= args.maximum_route_m
                    and endpoint_error
                    <= args.maximum_endpoint_error_m
                ):
                    candidates.append(
                        (
                            round(length, 2),
                            round(endpoint_error, 2),
                            target_index,
                            round(transform.location.x, 2),
                            round(transform.location.y, 2),
                            round(transform.location.z, 2),
                        )
                    )
            candidates.sort()
            print(
                "\n{}：出生点 {}，候选目标"
                "（路径米, 终点误差米, 编号, x, y, z）".format(
                    vehicle.vehicle_id, origin_index
                )
            )
            for item in candidates[: args.limit]:
                print("  {}".format(item))

        if args.common_vehicle_ids:
            unknown = [
                vehicle_id
                for vehicle_id in args.common_vehicle_ids
                if vehicle_id not in route_metrics
            ]
            if unknown:
                raise SystemExit(
                    "Unknown vehicle ids: {}".format(", ".join(unknown))
                )
            common = []
            for target_index in range(len(spawn_points)):
                metrics = [
                    route_metrics[vehicle_id].get(target_index)
                    for vehicle_id in args.common_vehicle_ids
                ]
                if any(metric is None for metric in metrics):
                    continue
                route_lengths = [metric[0] for metric in metrics]
                endpoint_errors = [metric[2] for metric in metrics]
                if not all(
                    args.minimum_route_m
                    <= length
                    <= args.maximum_route_m
                    for length in route_lengths
                ):
                    continue
                if not all(
                    error <= args.maximum_endpoint_error_m
                    for error in endpoint_errors
                ):
                    continue
                common.append(
                    (
                        round(max(route_lengths), 2),
                        round(sum(route_lengths), 2),
                        target_index,
                        [
                            "{}:route={:.2f},direct={:.2f},end_error={:.2f}".format(
                                vehicle_id,
                                metric[0],
                                metric[1],
                                metric[2],
                            )
                            for vehicle_id, metric in zip(
                                args.common_vehicle_ids, metrics
                            )
                        ],
                    )
                )
            common.sort()
            print(
                "\n共同可达候选（最大路径米, 路径总和米, 编号, "
                "各车路径/直线距离/终点误差）"
            )
            for item in common[: args.limit]:
                print("  {}".format(item))
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
