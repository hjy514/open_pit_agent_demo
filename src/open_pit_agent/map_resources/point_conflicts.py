"""P4 point-conflict inference from P3-verified spawn-point facts.

Static centre-distance inference is deliberately conservative and is not a
substitute for a two-vehicle CARLA spawn test, collision test, or traversal.
"""

import math


CONFLICT_TYPE_STATIC_DISTANCE = "STATIC_CENTER_DISTANCE"
SOURCE_STATIC_DISTANCE = "P4_STATIC_CENTER_DISTANCE"
STATUS_STATIC_INFERRED = "STATIC_INFERRED"


def _distance_m(first, second):
    return math.sqrt(
        (float(first["x"]) - float(second["x"])) ** 2
        + (float(first["y"]) - float(second["y"])) ** 2
        + (float(first["z"]) - float(second["z"])) ** 2
    )


def infer_static_point_conflicts(store, map_id, resource_version,
                                 minimum_center_distance_m):
    """Infer candidate conflicts among P3 verified points using an explicit threshold.

    The threshold is a caller-selected policy value.  It is persisted in the
    record and must not be interpreted as a measured `vehicle.cat.cat`
    footprint or proof of a collision.
    """
    threshold = float(minimum_center_distance_m)
    if threshold <= 0:
        raise ValueError("minimum_center_distance_m must be positive")
    points = list(store.verified_spawn_points(map_id))
    records = []
    evaluated_pairs = 0
    for index, point_a in enumerate(points):
        for point_b in points[index + 1:]:
            evaluated_pairs += 1
            distance_m = _distance_m(point_a, point_b)
            if distance_m >= threshold:
                continue
            records.append({
                "map_id": map_id,
                "resource_version": resource_version,
                "point_a_id": point_a["point_id"],
                "point_b_id": point_b["point_id"],
                "conflict_type": CONFLICT_TYPE_STATIC_DISTANCE,
                "minimum_clearance_m": distance_m,
                "validation_status": STATUS_STATIC_INFERRED,
                "source": SOURCE_STATIC_DISTANCE,
                "notes": (
                    "P4 static centre-distance inference: {:.3f}m < configured "
                    "policy threshold {:.3f}m; no two-vehicle CARLA test performed."
                ).format(distance_m, threshold),
            })
    # This function computes the complete static result for one threshold.
    # Remove the previous static inference for the same map/version so a
    # smaller threshold cannot leave stale candidate conflicts behind.
    store.connection.execute(
        """
        DELETE FROM point_conflicts
        WHERE map_id = ? AND resource_version = ?
          AND conflict_type = ?
        """,
        (str(map_id), str(resource_version), CONFLICT_TYPE_STATIC_DISTANCE),
    )
    store.upsert_point_conflicts(records)
    return {
        "verified_spawn_points": len(points),
        "evaluated_pairs": evaluated_pairs,
        "candidate_conflicts": len(records),
        "minimum_center_distance_m": threshold,
    }
