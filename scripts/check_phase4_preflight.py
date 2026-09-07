#!/usr/bin/env python3
"""Read-only preflight for the P4 CARLA dual-spawn validation command."""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.dual_spawn_calibrator import ensure_no_existing_vehicles


def map_matches(actual_name, expected_name):
    actual = str(actual_name).replace("\\", "/").rstrip("/")
    expected = str(expected_name).replace("\\", "/").rstrip("/")
    return actual == expected or actual.endswith("/" + expected)


def resource_counts(store, map_id, resource_version):
    verified = store.connection.execute(
        "SELECT count(*) FROM map_points WHERE map_id=? AND validation_status='VERIFIED_SPAWN'",
        (map_id,),
    ).fetchone()[0]
    candidates = store.connection.execute(
        """
        SELECT count(*)
        FROM point_conflicts AS c
        JOIN map_points AS a ON a.point_id = c.point_a_id
        JOIN map_points AS b ON b.point_id = c.point_b_id
        WHERE c.map_id=? AND c.resource_version=?
          AND c.conflict_type='STATIC_CENTER_DISTANCE'
          AND c.validation_status='STATIC_INFERRED'
          AND a.validation_status='VERIFIED_SPAWN'
          AND b.validation_status='VERIFIED_SPAWN'
          AND NOT EXISTS (
              SELECT 1 FROM point_conflicts AS checked
              WHERE checked.map_id = c.map_id
                AND checked.resource_version = c.resource_version
                AND checked.point_a_id = c.point_a_id
                AND checked.point_b_id = c.point_b_id
                AND checked.conflict_type='DUAL_HEAVY_TRUCK_SPAWN_CHECK'
          )
        """,
        (map_id, resource_version),
    ).fetchone()[0]
    return {"verified_spawn_points": verified, "static_candidate_conflicts": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "mine_competition_demo.json")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--expected-map-name", default="0325_5")
    parser.add_argument("--expected-carla-version", default="0.9.10")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--pair-limit", type=int, default=10, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        adapter = CarlaAdapter(load_config(args.config))
        adapter._import_carla()
    except CarlaAdapterError as exc:
        raise RuntimeError("无法加载 CARLA Python API；请检查 OPENPIT_CARLA_ROOT") from exc
    client = adapter.carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)
    server_version = client.get_server_version()
    world = client.get_world()
    ensure_no_existing_vehicles(world)
    actual_map_name = world.get_map().name
    if args.expected_carla_version not in server_version:
        raise RuntimeError("CARLA version mismatch: expected {}, got {}".format(args.expected_carla_version, server_version))
    if not map_matches(actual_map_name, args.expected_map_name):
        raise RuntimeError("CARLA map mismatch: expected {}, got {}".format(args.expected_map_name, actual_map_name))
    with MapResourceStore(args.database) as store:
        counts = resource_counts(store, args.map_id, args.resource_version)
    if counts["verified_spawn_points"] == 0:
        raise RuntimeError("P3 VERIFIED_SPAWN points are absent; do not run P4 dual spawn")
    if counts["static_candidate_conflicts"] == 0:
        raise RuntimeError("P4 STATIC_INFERRED candidates are absent; run static inference first")
    print("P4 preflight PASS")
    print("CARLA version: {}".format(server_version))
    print("CARLA map: {}".format(actual_map_name))
    print("P3 VERIFIED_SPAWN points: {}".format(counts["verified_spawn_points"]))
    print("P4 STATIC_INFERRED candidates: {}".format(counts["static_candidate_conflicts"]))
    print("Database: {}".format(args.database.resolve()))
    print("Boundary: preflight only; no Actor was spawned and no database row was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
