"""P4 simultaneous two-heavy-truck Spawn verification for selected point pairs."""

import time
import uuid


CALIBRATION_TYPE = "CARLA_DUAL_HEAVY_TRUCK_SPAWN"
SOURCE = "P4_CARLA_DUAL_HEAVY_TRUCK_SPAWN"
CONFLICT_TYPE_CHECK = "DUAL_HEAVY_TRUCK_SPAWN_CHECK"
STATUS_VERIFIED = "DUAL_SPAWN_VERIFIED"
STATUS_BLOCKED = "DUAL_SPAWN_BLOCKED"


def _map_matches(actual_name, expected_name):
    actual = str(actual_name).replace("\\", "/").rstrip("/")
    expected = str(expected_name).replace("\\", "/").rstrip("/")
    return actual == expected or actual.endswith("/" + expected)


def ensure_no_existing_vehicles(world):
    """Refuse calibration when unrelated vehicles could occupy test points."""
    get_actors = getattr(world, "get_actors", None)
    if not callable(get_actors):
        return
    actors = get_actors()
    vehicles = list(actors.filter("vehicle.*"))
    if vehicles:
        actor_ids = [str(getattr(actor, "id", "unknown")) for actor in vehicles]
        raise RuntimeError(
            "CARLA world contains {} existing vehicle actors (ids={}); "
            "P4 requires a clean world".format(len(vehicles), ",".join(actor_ids))
        )


def destroy_actor_safely(world, actor, confirmation_timeout_seconds=2.0):
    """Destroy one actor and verify ambiguous CARLA 0.9.10 False results.

    Some CARLA 0.9.10 builds return ``False`` even though the actor has
    disappeared from the world.  A false return is therefore confirmed
    against ``world.get_actor`` (or ``actor.is_alive``) before it is treated
    as a cleanup failure.  The return value reports that compatibility case.
    """
    destroyed = actor.destroy()
    if destroyed is not False:
        return False
    actor_id = getattr(actor, "id", None)
    get_actors = getattr(world, "get_actors", None)
    get_actor = getattr(world, "get_actor", None)
    if actor_id is not None and callable(get_actors):
        # CARLA 0.9.10 custom builds can keep returning a stale proxy from
        # world.get_actor(id) after the actor is no longer alive or present
        # in the current world actor list.  The live list is authoritative.
        deadline = time.monotonic() + max(0.0, float(confirmation_timeout_seconds))
        while True:
            listed = any(
                getattr(item, "id", None) == actor_id
                for item in get_actors().filter("vehicle.*")
            )
            if not listed:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise RuntimeError("actor.destroy() returned False and actor still exists")
    if actor_id is not None and callable(get_actor):
        deadline = time.monotonic() + max(0.0, float(confirmation_timeout_seconds))
        while True:
            if get_actor(actor_id) is None:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise RuntimeError("actor.destroy() returned False and actor still exists")
    is_alive = getattr(actor, "is_alive", None)
    if is_alive is False:
        return True
    raise RuntimeError("actor.destroy() returned False and absence could not be confirmed")


def select_static_candidates(store, map_id, resource_version, limit):
    """Choose shortest P4 static candidates; selection is deterministic."""
    rows = store.connection.execute(
        """
        SELECT c.point_a_id, c.point_b_id, c.minimum_clearance_m,
               a.carla_spawn_point_index, a.x, a.y, a.z, a.yaw,
               b.carla_spawn_point_index, b.x, b.y, b.z, b.yaw
        FROM point_conflicts AS c
        JOIN map_points AS a ON a.point_id = c.point_a_id
        JOIN map_points AS b ON b.point_id = c.point_b_id
        WHERE c.map_id = ? AND c.resource_version = ?
          AND c.conflict_type = 'STATIC_CENTER_DISTANCE'
          AND c.validation_status = 'STATIC_INFERRED'
          AND a.validation_status = 'VERIFIED_SPAWN'
          AND b.validation_status = 'VERIFIED_SPAWN'
          AND NOT EXISTS (
              SELECT 1 FROM point_conflicts AS checked
              WHERE checked.map_id = c.map_id
                AND checked.resource_version = c.resource_version
                AND checked.point_a_id = c.point_a_id
                AND checked.point_b_id = c.point_b_id
                AND checked.conflict_type = 'DUAL_HEAVY_TRUCK_SPAWN_CHECK'
          )
        ORDER BY c.minimum_clearance_m, c.point_a_id, c.point_b_id
        LIMIT ?
        """,
        (map_id, resource_version, int(limit)),
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "point_a_id": row[0], "point_b_id": row[1], "distance_m": row[2],
            "point_a": {
                "spawn_point_index": row[3],
                "x": row[4], "y": row[5], "z": row[6], "yaw": row[7],
            },
            "point_b": {
                "spawn_point_index": row[8],
                "x": row[9], "y": row[10], "z": row[11], "yaw": row[12],
            },
        })
    return result


def verify_dual_spawn_pairs(store, world, transform_factory, map_id, resource_version,
                            expected_map_name, pairs, vehicle_blueprint="vehicle.cat.cat",
                            run_id=None, carla_server_version=None):
    """Try both spawns simultaneously and persist the limited P4 outcome.

    A dual-spawn success proves only that both actors could be created together
    at that instant.  It does not prove collision-free driving, clearance, or
    capacity for a larger fleet.
    """
    carla_map = world.get_map()
    if expected_map_name and not _map_matches(carla_map.name, expected_map_name):
        raise RuntimeError("CARLA world map mismatch: expected {}, got {}".format(
            expected_map_name, carla_map.name))
    ensure_no_existing_vehicles(world)
    selected = list(pairs)
    run_id = run_id or "dual-spawn-calibration-{}".format(uuid.uuid4().hex)
    store.start_calibration_run(
        run_id, map_id, resource_version, CALIBRATION_TYPE, "phase4-v1", SOURCE,
        "P4 simultaneous pair spawn only; no traversal, collision or clearance validation.",
    )
    results = []
    cleanup_compatibility_warnings = 0
    try:
        blueprint = world.get_blueprint_library().find(vehicle_blueprint)
        for pair in selected:
            first_actor = second_actor = None
            outcome = STATUS_BLOCKED
            reason = None
            infrastructure_error = None
            try:
                first_actor = world.try_spawn_actor(blueprint, transform_factory(pair["point_a"]))
                if first_actor is None:
                    reason = "POINT_A_SPAWN_FAILED"
                else:
                    second_actor = world.try_spawn_actor(blueprint, transform_factory(pair["point_b"]))
                    if second_actor is None:
                        reason = "POINT_B_SPAWN_FAILED"
                    else:
                        outcome = STATUS_VERIFIED
            except Exception as exc:
                infrastructure_error = "SPAWN_EXCEPTION: {}".format(exc)
            finally:
                for actor in (second_actor, first_actor):
                    if actor is not None:
                        try:
                            if destroy_actor_safely(world, actor):
                                cleanup_compatibility_warnings += 1
                        except Exception as exc:
                            infrastructure_error = "ACTOR_DESTROY_FAILED: {}".format(exc)
            if infrastructure_error is not None:
                # RPC/world failures are not evidence that the spatial pair
                # conflicts. Abort this calibration run without writing a
                # false DUAL_SPAWN_BLOCKED conclusion for the current pair.
                raise RuntimeError(infrastructure_error)
            notes = (
                "P4 dual vehicle.cat.cat spawn succeeded; route, collision, clearance "
                "and fleet-capacity validation pending."
                if outcome == STATUS_VERIFIED else "P4 dual spawn blocked: {}".format(reason)
            )
            results.append({
                "map_id": map_id, "resource_version": resource_version,
                "point_a_id": pair["point_a_id"], "point_b_id": pair["point_b_id"],
                "conflict_type": CONFLICT_TYPE_CHECK,
                "minimum_clearance_m": pair.get("distance_m"),
                "validation_status": outcome, "source": SOURCE, "notes": notes,
            })
            store.upsert_point_conflicts([results[-1]])
        verified = sum(1 for item in results if item["validation_status"] == STATUS_VERIFIED)
        summary = {
            "pairs_requested": len(selected), "dual_spawn_verified": verified,
            "dual_spawn_blocked": len(selected) - verified,
            "actual_carla_map_name": carla_map.name,
            "carla_server_version": carla_server_version,
            "resource_version": resource_version,
            "vehicle_blueprint": vehicle_blueprint,
            "cleanup_compatibility_warnings": cleanup_compatibility_warnings,
        }
        store.finish_calibration_run(run_id, "PASS", summary)
        return {"calibration_run_id": run_id, **summary}
    except Exception as exc:
        store.finish_calibration_run(run_id, "FAILED", {
            "pairs_recorded": len(results), "error": str(exc),
            "actual_carla_map_name": carla_map.name,
            "carla_server_version": carla_server_version,
            "resource_version": resource_version,
        })
        raise
