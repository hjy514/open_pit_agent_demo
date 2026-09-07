"""Versioned registration of declarative mine operating-area profiles."""
import json
from pathlib import Path
from typing import Any, Dict, Iterable


def load_operating_area_profile(path: Path) -> Dict[str, Any]:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    required = ("schema_version", "profile_id", "map_id", "resource_version", "source", "areas")
    missing = [key for key in required if raw.get(key) is None]
    if missing:
        raise ValueError("operating area profile missing: {}".format(", ".join(missing)))
    if not isinstance(raw["areas"], list) or not raw["areas"]:
        raise ValueError("operating area profile requires non-empty areas")
    return raw


def area_records(profile: Dict[str, Any]) -> Iterable[Dict[str, object]]:
    """Convert profile rows into normalized map-resource database records."""
    seen = set()
    for area in profile["areas"]:
        area_id = str(area.get("area_id", ""))
        indices = area.get("point_spawn_indices", [])
        if not area_id or area_id in seen:
            raise ValueError("operating area ids must be non-empty and unique")
        if not isinstance(indices, list) or not indices:
            raise ValueError("area {} requires point_spawn_indices".format(area_id))
        capacity = int(area.get("capacity", 0))
        if capacity < 0:
            raise ValueError("area {} capacity cannot be negative".format(area_id))
        seen.add(area_id)
        yield {
            "area_id": area_id,
            "map_id": str(profile["map_id"]),
            "resource_version": str(profile["resource_version"]),
            "display_name": str(area["display_name"]),
            "area_type": str(area["area_type"]),
            "selection_mode": str(area["selection_mode"]),
            "capacity": capacity,
            "allowed_roles": [str(value) for value in area.get("allowed_roles", [])],
            "point_ids": ["carla-spawn:{}".format(int(index)) for index in indices],
            "validation_status": str(area["validation_status"]),
            "source": str(profile["source"]),
            "notes": str(area.get("notes", "")),
        }


def register_operating_area_profile(store: Any, profile: Dict[str, Any]) -> int:
    """Persist only declarative area semantics into the map resource library."""
    records = list(area_records(profile))
    if not store.has_map_resource_version(
        str(profile["map_id"]), str(profile["resource_version"])
    ):
        raise ValueError("map resource version is not registered for operating-area profile")
    all_point_ids = {
        point_id for record in records for point_id in record["point_ids"]
    }
    placeholders = ",".join("?" for _ in all_point_ids)
    rows = store.connection.execute(
        "SELECT point_id FROM map_points WHERE map_id = ? AND point_id IN ({})".format(
            placeholders
        ),
        [str(profile["map_id"])] + sorted(all_point_ids),
    ).fetchall()
    known = {str(row[0]) for row in rows}
    missing = sorted(all_point_ids - known)
    if missing:
        raise ValueError("operating-area profile references unknown map points: {}".format(", ".join(missing)))
    return store.upsert_operating_areas(records)
