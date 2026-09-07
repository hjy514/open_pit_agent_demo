"""Phase 3 single-heavy-truck CARLA spawn calibration.

This module deliberately verifies only actor creation and basic waypoint
context.  It does not establish route reachability, clearance, collision
safety, or multi-vehicle compatibility.
"""

import uuid


CALIBRATION_TYPE = "CARLA_SINGLE_HEAVY_TRUCK_SPAWN"
SOURCE = "CARLA_SINGLE_HEAVY_TRUCK_SPAWN_CALIBRATION"
TOOL_VERSION = "phase3-v1"
VERIFIED_SPAWN = "VERIFIED_SPAWN"
EXCLUDED = "EXCLUDED"


def _map_matches(actual_name, expected_name):
    actual = str(actual_name).replace("\\", "/").rstrip("/")
    expected = str(expected_name).replace("\\", "/").rstrip("/")
    return actual == expected or actual.endswith("/" + expected)


def _lane_type_name(waypoint):
    lane_type = getattr(waypoint, "lane_type", None)
    return str(lane_type) if lane_type is not None else None


def _is_driving_lane(waypoint):
    lane_type = _lane_type_name(waypoint)
    return lane_type is not None and "Driving" in lane_type


def _vertical_offset_m(transform, waypoint):
    """Return the Spawn/waypoint Z-reference delta when it is available.

    Custom CARLA maps may expose a systematic offset between a spawn
    transform and OpenDRIVE waypoint geometry.  The value is therefore a
    P3 data-quality observation, not a safety verdict by itself.
    """
    waypoint_transform = getattr(waypoint, "transform", None)
    waypoint_location = getattr(waypoint_transform, "location", None)
    if waypoint_location is None:
        return None
    return abs(float(transform.location.z) - float(waypoint_location.z))


def _transform_record(index, transform, waypoint):
    location = transform.location
    rotation = transform.rotation
    return {
        "spawn_point_index": index,
        "x": float(location.x), "y": float(location.y), "z": float(location.z),
        "yaw": float(rotation.yaw),
        "road_id": str(getattr(waypoint, "road_id", "")) or None,
        "lane_id": getattr(waypoint, "lane_id", None),
        "s": getattr(waypoint, "s", None),
        "lane_type": _lane_type_name(waypoint),
        "travel_direction": None,
        "source": SOURCE,
    }


def calibrate_spawn_points(store, world, map_id, resource_version, expected_map_name,
                           vehicle_blueprint="vehicle.cat.cat", run_id=None,
                           maximum_z_offset_m=2.0, carla_server_version=None):
    """Calibrate every CARLA spawn point and persist its latest basic result.

    ``world`` is injected so unit tests can exercise lifecycle and failure
    handling without importing CARLA.  Every temporary actor is destroyed in
    a ``finally`` block before the point result is persisted.
    """
    carla_map = world.get_map()
    if expected_map_name and not _map_matches(carla_map.name, expected_map_name):
        raise RuntimeError("CARLA world map mismatch: expected {}, got {}".format(
            expected_map_name, carla_map.name))
    run_id = run_id or "spawn-calibration-{}".format(uuid.uuid4().hex)
    store.start_calibration_run(
        run_id, map_id, resource_version, CALIBRATION_TYPE, TOOL_VERSION, SOURCE,
        "P3: single vehicle.cat.cat spawn only; no route or multi-vehicle validation.",
    )
    records = []
    failure_reasons = {}
    try:
        blueprints = world.get_blueprint_library()
        blueprint = blueprints.find(vehicle_blueprint)
        spawn_points = carla_map.get_spawn_points()
        for index, transform in enumerate(spawn_points):
            actor = None
            waypoint = None
            try:
                # Every listed CARLA Spawn Point receives one generation attempt.
                # Lane context then decides whether that successful spawn is
                # eligible for the P3 verified pool.
                actor = world.try_spawn_actor(blueprint, transform)
                if actor is None:
                    raise ValueError("SPAWN_FAILED")
                waypoint = carla_map.get_waypoint(transform.location, project_to_road=False)
                if waypoint is None:
                    raise ValueError("NO_WAYPOINT")
                record = _transform_record(index, transform, waypoint)
                if not _is_driving_lane(waypoint):
                    raise ValueError("NOT_DRIVING_LANE")
                vertical_offset_m = _vertical_offset_m(transform, waypoint)
                notes = "Single vehicle.cat.cat generated; basic driving-lane check passed."
                if (
                    vertical_offset_m is not None
                    and vertical_offset_m > maximum_z_offset_m
                ):
                    notes += (
                        " ELEVATION_REFERENCE_WARNING: Spawn/waypoint Z delta={:.3f}m; "
                        "recorded for later traversal validation, not excluded in P3."
                    ).format(vertical_offset_m)
                record.update({
                    "heavy_truck_allowed": 1,
                    "validation_status": VERIFIED_SPAWN,
                    "nearest_point_distance_m": vertical_offset_m,
                    "notes": notes,
                })
            except Exception as exc:
                if waypoint is None:
                    # Keep an inspectable point record even if CARLA cannot project it.
                    record = _transform_record(index, transform, type("NoWaypoint", (), {})())
                reason = str(exc) or exc.__class__.__name__
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
                record.update({
                    "heavy_truck_allowed": 0,
                    "validation_status": EXCLUDED,
                    "notes": "P3 excluded: {}".format(reason),
                })
            finally:
                if actor is not None:
                    try:
                        actor.destroy()
                    except Exception as destroy_error:
                        record["validation_status"] = EXCLUDED
                        record["heavy_truck_allowed"] = 0
                        record["notes"] = "P3 excluded: ACTOR_DESTROY_FAILED: {}".format(destroy_error)
                        failure_reasons["ACTOR_DESTROY_FAILED"] = failure_reasons.get("ACTOR_DESTROY_FAILED", 0) + 1
            records.append(record)
            store.upsert_spawn_calibration_points(map_id, [record])
        verified = sum(1 for record in records if record["validation_status"] == VERIFIED_SPAWN)
        summary = {"spawn_points": len(records), "verified_spawn": verified,
                   "excluded": len(records) - verified, "failure_reasons": failure_reasons,
                   "vehicle_blueprint": vehicle_blueprint,
                   "maximum_z_offset_m": maximum_z_offset_m,
                   "actual_carla_map_name": carla_map.name,
                   "resource_version": resource_version,
                   "carla_server_version": carla_server_version}
        store.finish_calibration_run(run_id, "PASS", summary)
        return {"calibration_run_id": run_id, **summary}
    except Exception as exc:
        summary = {"spawn_points_recorded": len(records), "error": str(exc),
                   "vehicle_blueprint": vehicle_blueprint,
                   "actual_carla_map_name": carla_map.name,
                   "resource_version": resource_version,
                   "carla_server_version": carla_server_version}
        store.finish_calibration_run(run_id, "FAILED", summary)
        raise
