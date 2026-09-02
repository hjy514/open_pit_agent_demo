#!/usr/bin/env python3
"""Capture CARLA GlobalRoutePlanner candidates as Map Resource JSON.

This script is intended for Ubuntu + CARLA 0.9.10 (Python 3.7). It is not
executed on machines without CARLA. Captured routes are marked
``CARLA_CAPTURED_UNVERIFIED`` until a separate traversal/clearance check is
completed.
"""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path


def _canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_route_endpoints(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("route_endpoints", payload)
    if not isinstance(payload, dict):
        raise ValueError("route endpoint input must be an object")
    result = {}
    for task_id, spec in payload.items():
        if not isinstance(spec, dict):
            raise ValueError("route endpoint {} must be an object".format(task_id))
        if "from_spawn_point_index" not in spec or "to_spawn_point_index" not in spec:
            raise ValueError("route endpoint {} needs from/to_spawn_point_index".format(task_id))
        result[str(task_id)] = {
            "from_spawn_point_index": int(spec["from_spawn_point_index"]),
            "to_spawn_point_index": int(spec["to_spawn_point_index"]),
            "from_point_id": spec.get("from_point_id"),
            "to_point_id": spec.get("to_point_id"),
        }
    return result


def route_payload(route):
    """Convert CARLA waypoint tuples into JSON-safe static route facts."""
    waypoints = []
    total_length = 0.0
    previous = None
    for item in route:
        waypoint = item[0] if isinstance(item, (tuple, list)) else item
        location = waypoint.transform.location
        current = {"x": float(location.x), "y": float(location.y), "z": float(location.z)}
        if previous is not None:
            total_length += math.sqrt(
                (current["x"] - previous["x"]) ** 2
                + (current["y"] - previous["y"]) ** 2
                + (current["z"] - previous["z"]) ** 2
            )
        previous = current
        waypoints.append(
            {
                "location": current,
                "road_id": int(getattr(waypoint, "road_id", 0)),
                "lane_id": int(getattr(waypoint, "lane_id", 0)),
                "s": float(getattr(waypoint, "s", 0.0)),
                "is_junction": bool(getattr(waypoint, "is_junction", False)),
            }
        )
    sequence = {"waypoints": waypoints, "edge_ids": []}
    return sequence, total_length, _canonical_hash(sequence)


def capture_routes(args):
    try:
        import carla
        from agents.navigation.global_route_planner import GlobalRoutePlanner
    except ImportError as exc:
        raise RuntimeError(
            "CARLA 0.9.10 Python API and agents package are required on the Ubuntu validation computer"
        ) from exc

    endpoints = load_route_endpoints(args.input)
    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    world = client.get_world()
    if args.expected_map_name and world.get_map().name != args.expected_map_name:
        raise RuntimeError(
            "CARLA world map mismatch: expected {}, got {}".format(
                args.expected_map_name, world.get_map().name
            )
        )
    spawn_points = world.get_map().get_spawn_points()
    planner = GlobalRoutePlanner(world.get_map(), args.sampling_resolution)
    records = []
    for task_id, spec in endpoints.items():
        start_index = spec["from_spawn_point_index"]
        target_index = spec["to_spawn_point_index"]
        if not (0 <= start_index < len(spawn_points)) or not (0 <= target_index < len(spawn_points)):
            raise ValueError("{} references a spawn point outside 0..{}".format(task_id, len(spawn_points) - 1))
        route = planner.trace_route(
            spawn_points[start_index].location, spawn_points[target_index].location
        )
        sequence, length_m, route_hash = route_payload(route)
        records.append(
            {
                "route_candidate_id": "{}-carla-{}".format(task_id, route_hash[:12]),
                "map_id": args.map_id,
                "resource_version": args.resource_version,
                "from_point_id": spec.get("from_point_id") or "carla-spawn:{}".format(start_index),
                "to_point_id": spec.get("to_point_id") or "carla-spawn:{}".format(target_index),
                "candidate_rank": 1,
                "planner_version": "CARLA_GlobalRoutePlanner",
                "route_hash": route_hash,
                "route_length_m": length_m,
                "junction_count": sum(1 for item in sequence["waypoints"] if item["is_junction"]),
                "road_lane_sequence": sequence,
                "validation_status": "CARLA_CAPTURED_UNVERIFIED",
                "source": "CARLA_GLOBAL_ROUTE_PLANNER",
                "notes": "Route capture only; traversal and heavy-truck clearance validation pending",
            }
        )
    return {"route_candidates": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="route_endpoints JSON")
    parser.add_argument("--output", type=Path, default=Path("route_candidates.carla.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sampling-resolution", type=float, default=2.0)
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="")
    args = parser.parse_args()
    payload = capture_routes(args)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Captured {} route candidates to {}".format(len(payload["route_candidates"]), args.output))
    print("Status: CARLA_CAPTURED_UNVERIFIED; traversal and clearance validation are still required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
