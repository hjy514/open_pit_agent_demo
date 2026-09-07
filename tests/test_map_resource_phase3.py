import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.carla_calibrator import calibrate_spawn_points


class Location:
    def __init__(self, x, y, z=0.0): self.x, self.y, self.z = x, y, z


class Rotation:
    def __init__(self, yaw=0.0): self.yaw = yaw


class Transform:
    def __init__(self, x, y): self.location, self.rotation = Location(x, y), Rotation()


class Waypoint:
    def __init__(self, driving=True):
        self.road_id, self.lane_id, self.s = 7, -1, 2.0
        self.lane_type = "Driving" if driving else "Sidewalk"
        self.transform = Transform(0, 0)


class Actor:
    def __init__(self): self.destroyed = False
    def destroy(self): self.destroyed = True


class FakeMap:
    name = "/Game/Carla/Maps/0325_5"
    def __init__(self): self.points = [Transform(1, 2), Transform(3, 4), Transform(5, 6)]
    def get_spawn_points(self): return self.points
    def get_waypoint(self, location, project_to_road=False):
        return None if location.x == 5 else Waypoint(driving=location.x == 1)


class FakeBlueprints:
    def find(self, value): return value


class FakeWorld:
    def __init__(self): self.map, self.actors = FakeMap(), []
    def get_map(self): return self.map
    def get_blueprint_library(self): return FakeBlueprints()
    def try_spawn_actor(self, blueprint, transform):
        if transform.location.x == 3: return None
        actor = Actor(); self.actors.append(actor); return actor


class Phase3CalibrationTests(unittest.TestCase):
    def test_single_spawn_calibration_records_facts_and_destroys_actor(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat")
                world = FakeWorld()
                result = calibrate_spawn_points(store, world, "0325_5", "1.0-draft", "0325_5", run_id="run-1")
                self.assertEqual(3, result["spawn_points"])
                self.assertEqual(1, result["verified_spawn"])
                self.assertEqual(2, result["excluded"])
                self.assertTrue(world.actors[0].destroyed)
                rows = store.connection.execute("SELECT carla_spawn_point_index, validation_status FROM map_points ORDER BY carla_spawn_point_index").fetchall()
                self.assertEqual([(0, "VERIFIED_SPAWN"), (1, "EXCLUDED"), (2, "EXCLUDED")], rows)
                status = store.connection.execute("SELECT status FROM calibration_runs WHERE calibration_run_id = 'run-1'").fetchone()[0]
                self.assertEqual("PASS", status)
                summary = json.loads(store.connection.execute(
                    "SELECT summary_json FROM calibration_runs WHERE calibration_run_id = 'run-1'"
                ).fetchone()[0])
                self.assertEqual("/Game/Carla/Maps/0325_5", summary["actual_carla_map_name"])
                self.assertEqual("1.0-draft", summary["resource_version"])

    def test_map_mismatch_does_not_start_a_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat")
                with self.assertRaises(RuntimeError):
                    calibrate_spawn_points(store, FakeWorld(), "0325_5", "1.0-draft", "other-map")
                self.assertEqual(0, store.connection.execute("SELECT count(*) FROM calibration_runs").fetchone()[0])

    def test_systematic_waypoint_z_offset_is_recorded_as_warning_not_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat")
                world = FakeWorld()
                world.map.points[0].location.z = 5.0
                result = calibrate_spawn_points(
                    store, world, "0325_5", "1.0-draft", "0325_5",
                    run_id="run-z-warning", maximum_z_offset_m=2.0,
                )
                self.assertEqual(1, result["verified_spawn"])
                row = store.connection.execute(
                    "SELECT validation_status, nearest_point_distance_m, notes "
                    "FROM map_points WHERE carla_spawn_point_index = 0"
                ).fetchone()
                self.assertEqual("VERIFIED_SPAWN", row[0])
                self.assertEqual(5.0, row[1])
                self.assertIn("ELEVATION_REFERENCE_WARNING", row[2])
