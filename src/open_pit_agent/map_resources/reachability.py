"""P5 directed CARLA route-planner reachability calibration.

This module records whether CARLA's planner can produce a route between two
P3-verified spawn points.  It deliberately does not spawn or drive a vehicle;
planner reachability is not physical traversal or heavy-truck safety proof.
"""

import hashlib
import json
import math
import uuid


CALIBRATION_TYPE = "CARLA_PLANNER_REACHABILITY"
SOURCE = "P5_CARLA_GLOBAL_ROUTE_PLANNER"
STATUS_REACHABLE = "PLANNER_REACHABLE"
STATUS_UNREACHABLE = "PLANNER_UNREACHABLE"
STATUS_NEAR_ENDPOINT = "PLANNER_NEAR_ENDPOINT"
STATUS_ENDPOINT_MISMATCH = "PLANNER_ENDPOINT_MISMATCH"
STATUS_ERROR = "PLANNER_ERROR"


def map_matches(actual_name, expected_name):
    actual = str(actual_name).replace("\\", "/").rstrip("/")
    expected = str(expected_name).replace("\\", "/").rstrip("/")
    return actual == expected or actual.endswith("/" + expected)


def _location_distance(first, second):
    return math.sqrt(
        (float(first.x) - float(second.x)) ** 2
        + (float(first.y) - float(second.y)) ** 2
        + (float(first.z) - float(second.z)) ** 2
    )


def _route_waypoint(item):
    return item[0] if isinstance(item, (tuple, list)) else item


def route_facts(route, target_location):
    """Calculate compact, deterministic facts without storing all waypoints."""
    waypoints = [_route_waypoint(item) for item in route]
    if not waypoints:
        return {
            "route_length_m": None,
            "endpoint_error_m": None,
            "junction_count": 0,
            "route_hash": None,
        }
    total_length = 0.0
    previous_location = None
    signature = []
    junction_count = 0
    inside_junction = False
    for waypoint in waypoints:
        location = waypoint.transform.location
        if previous_location is not None:
            total_length += _location_distance(previous_location, location)
        previous_location = location
        is_junction = bool(getattr(waypoint, "is_junction", False))
        if is_junction and not inside_junction:
            junction_count += 1
        inside_junction = is_junction
        signature.append([
            int(getattr(waypoint, "road_id", 0)),
            int(getattr(waypoint, "lane_id", 0)),
            round(float(getattr(waypoint, "s", 0.0)), 3),
            round(float(location.x), 3),
            round(float(location.y), 3),
            round(float(location.z), 3),
        ])
    payload = json.dumps(signature, ensure_ascii=False, separators=(",", ":"))
    return {
        "route_length_m": total_length,
        "endpoint_error_m": _location_distance(previous_location, target_location),
        "junction_count": junction_count,
        "route_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def directed_point_pairs(points):
    """Return a stable list of all distinct directed P3 point pairs."""
    ordered = sorted(points, key=lambda item: (int(item["spawn_point_index"]), item["point_id"]))
    return [
        (origin, target)
        for origin in ordered
        for target in ordered
        if origin["point_id"] != target["point_id"]
    ]


def selected_directed_point_pairs(points, spawn_index_pairs):
    """Resolve an explicit, directed Spawn Point route profile.

    A profile is intentionally a subset of the full Cartesian product.  It
    lets a scenario validate only the routes it actually uses before a long
    all-map calibration is available.
    """
    points_by_index = {
        int(point["spawn_point_index"]): point for point in points
    }
    selected = []
    seen = set()
    for origin_index, target_index in spawn_index_pairs:
        origin_index = int(origin_index)
        target_index = int(target_index)
        if origin_index == target_index:
            raise ValueError("route profile cannot contain a self pair: {}".format(origin_index))
        if origin_index not in points_by_index or target_index not in points_by_index:
            raise ValueError(
                "route profile references a non-VERIFIED_SPAWN point: {} -> {}".format(
                    origin_index, target_index)
            )
        key = (origin_index, target_index)
        if key not in seen:
            selected.append((points_by_index[origin_index], points_by_index[target_index]))
            seen.add(key)
    if not selected:
        raise ValueError("route profile contains no directed point pairs")
    return selected


def calibrate_planner_reachability(
        store, carla_map, planner, map_id, resource_version,
        expected_map_name, planner_version="CARLA_GlobalRoutePlanner_0.9.10",
        endpoint_tolerance_m=5.0, near_endpoint_tolerance_m=15.0,
        pair_limit=None, force=False,
        run_id=None, carla_server_version=None, maximum_consecutive_errors=3,
        progress_callback=None, spawn_index_pairs=None):
    """Plan a resumable batch and persist directed reachability summaries."""
    if endpoint_tolerance_m < 0:
        raise ValueError("endpoint_tolerance_m must be non-negative")
    if near_endpoint_tolerance_m < endpoint_tolerance_m:
        raise ValueError("near_endpoint_tolerance_m must be >= endpoint_tolerance_m")
    if pair_limit is not None and int(pair_limit) <= 0:
        raise ValueError("pair_limit must be positive")
    if int(maximum_consecutive_errors) <= 0:
        raise ValueError("maximum_consecutive_errors must be positive")
    if expected_map_name and not map_matches(carla_map.name, expected_map_name):
        raise RuntimeError("CARLA world map mismatch: expected {}, got {}".format(
            expected_map_name, carla_map.name))

    points = list(store.verified_spawn_points(map_id))
    if len(points) < 2:
        raise RuntimeError("P5 requires at least two P3 VERIFIED_SPAWN points")
    spawn_points = carla_map.get_spawn_points()
    for point in points:
        index = int(point["spawn_point_index"])
        if not 0 <= index < len(spawn_points):
            raise RuntimeError(
                "database Spawn Point index {} is outside current CARLA map range".format(index)
            )

    all_pairs = directed_point_pairs(points)
    candidate_pairs = (
        selected_directed_point_pairs(points, spawn_index_pairs)
        if spawn_index_pairs is not None else all_pairs
    )
    terminal = set() if force else store.terminal_reachable_pair_keys(
        map_id, resource_version, planner_version
    )
    pending = [
        pair for pair in candidate_pairs
        if (pair[0]["point_id"], pair[1]["point_id"]) not in terminal
    ]
    selected = pending[:int(pair_limit)] if pair_limit is not None else pending
    run_id = run_id or "planner-reachability-{}".format(uuid.uuid4().hex)
    store.start_calibration_run(
        run_id, map_id, resource_version, CALIBRATION_TYPE, "phase5-v1", SOURCE,
        "Planner reachability only; no vehicle traversal, clearance, collision or fleet-safety validation.",
    )

    counts = {
        STATUS_REACHABLE: 0,
        STATUS_UNREACHABLE: 0,
        STATUS_NEAR_ENDPOINT: 0,
        STATUS_ENDPOINT_MISMATCH: 0,
        STATUS_ERROR: 0,
    }
    consecutive_errors = 0
    processed = 0
    try:
        for origin, target in selected:
            origin_index = int(origin["spawn_point_index"])
            target_index = int(target["spawn_point_index"])
            start_location = spawn_points[origin_index].location
            target_location = spawn_points[target_index].location
            record = {
                "map_id": map_id,
                "resource_version": resource_version,
                "from_point_id": origin["point_id"],
                "to_point_id": target["point_id"],
                "planner_version": planner_version,
                "reachable": False,
                "route_length_m": None,
                "endpoint_error_m": None,
                "junction_count": None,
                "route_hash": None,
                "validation_status": STATUS_ERROR,
                "source": SOURCE,
                "notes": None,
            }
            try:
                route = planner.trace_route(start_location, target_location)
                facts = route_facts(route, target_location)
                record.update(facts)
                if not route:
                    record["validation_status"] = STATUS_UNREACHABLE
                    record["notes"] = "CARLA planner returned an empty route"
                elif facts["endpoint_error_m"] > float(endpoint_tolerance_m):
                    if facts["endpoint_error_m"] <= float(near_endpoint_tolerance_m):
                        record["validation_status"] = STATUS_NEAR_ENDPOINT
                        record["notes"] = (
                            "Route endpoint error {:.3f}m is within near-endpoint review band "
                            "({:.3f}m, {:.3f}m]; do not use for autonomous task dispatch "
                            "until single-truck traversal review."
                        ).format(
                            facts["endpoint_error_m"], float(endpoint_tolerance_m),
                            float(near_endpoint_tolerance_m),
                        )
                    else:
                        record["validation_status"] = STATUS_ENDPOINT_MISMATCH
                        record["notes"] = "Route endpoint error {:.3f}m exceeds {:.3f}m".format(
                            facts["endpoint_error_m"], float(near_endpoint_tolerance_m))
                else:
                    record["reachable"] = True
                    record["validation_status"] = STATUS_REACHABLE
                    record["notes"] = (
                        "Planner route generated; physical heavy-truck traversal remains unverified"
                    )
                consecutive_errors = 0
            except Exception as exc:
                record["validation_status"] = STATUS_ERROR
                record["notes"] = "{}: {}".format(type(exc).__name__, exc)
                consecutive_errors += 1
            store.upsert_reachable_pairs([record])
            processed += 1
            counts[record["validation_status"]] += 1
            if progress_callback is not None:
                progress_callback(processed, len(selected), record)
            if consecutive_errors >= int(maximum_consecutive_errors):
                raise RuntimeError(
                    "planner produced {} consecutive errors; aborting to avoid false unreachable data".format(
                        consecutive_errors)
                )

        terminal_after = store.terminal_reachable_pair_keys(map_id, resource_version, planner_version)
        terminal_candidate_after = {
            key for key in terminal_after
            if key in {
                (origin["point_id"], target["point_id"])
                for origin, target in candidate_pairs
            }
        }
        summary = {
            "actual_carla_map_name": carla_map.name,
            "carla_server_version": carla_server_version,
            "planner_version": planner_version,
            "verified_spawn_points": len(points),
            "directed_pairs_total": len(candidate_pairs),
            "global_directed_pairs_total": len(all_pairs),
            "terminal_pairs_before_run": len(terminal.intersection({
                (origin["point_id"], target["point_id"])
                for origin, target in candidate_pairs
            })),
            "pairs_requested": len(selected),
            "pairs_processed": processed,
            "planner_reachable": counts[STATUS_REACHABLE],
            "planner_unreachable": counts[STATUS_UNREACHABLE],
            "planner_near_endpoint": counts[STATUS_NEAR_ENDPOINT],
            "planner_endpoint_mismatch": counts[STATUS_ENDPOINT_MISMATCH],
            "planner_error": counts[STATUS_ERROR],
            "terminal_pairs_after_run": len(terminal_candidate_after),
            "remaining_pairs": len(candidate_pairs) - len(terminal_candidate_after),
            "complete": len(terminal_candidate_after) == len(candidate_pairs),
            "endpoint_tolerance_m": float(endpoint_tolerance_m),
            "near_endpoint_tolerance_m": float(near_endpoint_tolerance_m),
        }
        store.finish_calibration_run(run_id, "PASS", summary)
        summary["calibration_run_id"] = run_id
        return summary
    except BaseException as exc:
        summary = {
            "pairs_requested": len(selected),
            "pairs_processed": processed,
            "planner_reachable": counts[STATUS_REACHABLE],
            "planner_unreachable": counts[STATUS_UNREACHABLE],
            "planner_near_endpoint": counts[STATUS_NEAR_ENDPOINT],
            "planner_endpoint_mismatch": counts[STATUS_ENDPOINT_MISMATCH],
            "planner_error": counts[STATUS_ERROR],
            "error": "{}: {}".format(type(exc).__name__, exc),
        }
        store.finish_calibration_run(run_id, "FAILED", summary)
        raise
