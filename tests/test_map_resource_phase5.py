import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.reachability import (
    STATUS_ENDPOINT_MISMATCH,
    STATUS_NEAR_ENDPOINT,
    STATUS_REACHABLE,
    STATUS_UNREACHABLE,
    calibrate_planner_reachability,
    directed_point_pairs,
    route_facts,
    selected_directed_point_pairs,
)


class Location:
    def __init__(self, x, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class Transform:
    def __init__(self, location):
        self.location = location


class Waypoint:
    def __init__(self, x, road_id=1, lane_id=1, s=0.0, is_junction=False):
        self.transform = Transform(Location(x))
        self.road_id = road_id
        self.lane_id = lane_id
        self.s = s
        self.is_junction = is_junction


class Spawn:
    def __init__(self, x):
        self.location = Location(x)


class CarlaMap:
    name = "/Game/Carla/Maps/0325_5"

    def get_spawn_points(self):
        return [Spawn(0), Spawn(10), Spawn(20)]


class Planner:
    def __init__(self):
        self.calls = []

    def trace_route(self, start, target):
        self.calls.append((start.x, target.x))
        if target.x == 20:
            return []
        if start.x == 10 and target.x == 0:
            return [Waypoint(10), Waypoint(8)]
        return [Waypoint(start.x), Waypoint(target.x, s=target.x)]


def seed_points(store):
    store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat", resource_version="v")
    store.upsert_spawn_calibration_points("0325_5", [
        {"spawn_point_index": 0, "x": 0, "y": 0, "z": 0,
         "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
        {"spawn_point_index": 1, "x": 10, "y": 0, "z": 0,
         "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
        {"spawn_point_index": 2, "x": 20, "y": 0, "z": 0,
         "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
    ])


class Phase5ReachabilityTests(unittest.TestCase):
    def test_route_facts_are_compact_and_count_junction_entries(self):
        result = route_facts([
            Waypoint(0), Waypoint(2, is_junction=True),
            Waypoint(4, is_junction=True), Waypoint(6),
            Waypoint(8, is_junction=True), Waypoint(10),
        ], Location(10))
        self.assertAlmostEqual(10.0, result["route_length_m"])
        self.assertAlmostEqual(0.0, result["endpoint_error_m"])
        self.assertEqual(2, result["junction_count"])
        self.assertEqual(64, len(result["route_hash"]))

    def test_directed_pairs_are_stable_and_exclude_self_pairs(self):
        points = [
            {"point_id": "p2", "spawn_point_index": 2},
            {"point_id": "p0", "spawn_point_index": 0},
            {"point_id": "p1", "spawn_point_index": 1},
        ]
        pairs = directed_point_pairs(points)
        self.assertEqual(6, len(pairs))
        self.assertEqual(("p0", "p1"), (pairs[0][0]["point_id"], pairs[0][1]["point_id"]))

    def test_explicit_route_profile_selects_only_requested_pairs(self):
        points = [
            {"point_id": "p2", "spawn_point_index": 2},
            {"point_id": "p0", "spawn_point_index": 0},
            {"point_id": "p1", "spawn_point_index": 1},
        ]
        pairs = selected_directed_point_pairs(points, [(2, 0), (0, 1), (2, 0)])
        self.assertEqual(
            [("p2", "p0"), ("p0", "p1")],
            [(origin["point_id"], target["point_id"]) for origin, target in pairs],
        )
        with self.assertRaises(ValueError):
            selected_directed_point_pairs(points, [(0, 99)])

    def test_batch_is_persisted_and_second_run_resumes_terminal_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                seed_points(store)
                first = calibrate_planner_reachability(
                    store, CarlaMap(), Planner(), "0325_5", "v", "0325_5",
                    planner_version="test-planner", endpoint_tolerance_m=1.0,
                    pair_limit=3, run_id="p5-first",
                )
                self.assertEqual(3, first["pairs_processed"])
                self.assertEqual(3, first["remaining_pairs"])
                statuses = store.reachable_pair_summary("0325_5", "v", "test-planner")
                self.assertEqual(1, statuses[STATUS_REACHABLE])
                self.assertEqual(1, statuses[STATUS_UNREACHABLE])
                self.assertEqual(1, statuses[STATUS_NEAR_ENDPOINT])

                second_planner = Planner()
                second = calibrate_planner_reachability(
                    store, CarlaMap(), second_planner, "0325_5", "v", "0325_5",
                    planner_version="test-planner", endpoint_tolerance_m=1.0,
                    pair_limit=3, run_id="p5-second",
                )
                self.assertTrue(second["complete"])
                self.assertEqual(3, len(second_planner.calls))
                self.assertEqual(6, store.connection.execute(
                    "SELECT count(*) FROM reachable_pairs"
                ).fetchone()[0])

    def test_near_endpoint_is_not_marked_reachable(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                seed_points(store)
                result = calibrate_planner_reachability(
                    store, CarlaMap(), Planner(), "0325_5", "v", "0325_5",
                    planner_version="test-planner", endpoint_tolerance_m=5.0,
                    near_endpoint_tolerance_m=15.0,
                    pair_limit=4, run_id="p5-mismatch",
                )
                self.assertEqual(1, result["planner_near_endpoint"])
                row = store.connection.execute(
                    "SELECT reachable, validation_status FROM reachable_pairs "
                    "WHERE from_point_id='carla-spawn:1' AND to_point_id='carla-spawn:0'"
                ).fetchone()
                self.assertEqual((0, STATUS_NEAR_ENDPOINT), row)

    def test_explicit_route_profile_has_its_own_completion_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                seed_points(store)
                result = calibrate_planner_reachability(
                    store, CarlaMap(), Planner(), "0325_5", "v", "0325_5",
                    planner_version="test-planner", endpoint_tolerance_m=1.0,
                    spawn_index_pairs=[(0, 1), (1, 0)], run_id="p5-profile",
                )
                self.assertTrue(result["complete"])
                self.assertEqual(2, result["directed_pairs_total"])
                self.assertEqual(6, result["global_directed_pairs_total"])
                self.assertEqual(2, store.connection.execute(
                    "SELECT count(*) FROM reachable_pairs"
                ).fetchone()[0])

    def test_large_endpoint_error_remains_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                seed_points(store)
                result = calibrate_planner_reachability(
                    store, CarlaMap(), Planner(), "0325_5", "v", "0325_5",
                    planner_version="test-planner", endpoint_tolerance_m=1.0,
                    near_endpoint_tolerance_m=1.5,
                    pair_limit=3, run_id="p5-large-error",
                )
                self.assertEqual(1, result["planner_endpoint_mismatch"])

    def test_store_upsert_is_idempotent_and_directional(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                seed_points(store)
                base = {
                    "map_id": "0325_5", "resource_version": "v",
                    "from_point_id": "carla-spawn:0", "to_point_id": "carla-spawn:1",
                    "planner_version": "p", "reachable": True,
                    "validation_status": STATUS_REACHABLE, "source": "TEST",
                }
                self.assertEqual(1, store.upsert_reachable_pairs([base]))
                changed = dict(base, reachable=False, validation_status=STATUS_UNREACHABLE)
                self.assertEqual(1, store.upsert_reachable_pairs([changed]))
                reverse = dict(base, from_point_id="carla-spawn:1", to_point_id="carla-spawn:0")
                store.upsert_reachable_pairs([reverse])
                self.assertEqual(2, store.connection.execute(
                    "SELECT count(*) FROM reachable_pairs"
                ).fetchone()[0])
                self.assertEqual(0, store.connection.execute(
                    "SELECT reachable FROM reachable_pairs WHERE from_point_id='carla-spawn:0'"
                ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
