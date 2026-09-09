import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore, infer_static_point_conflicts


class Phase4StaticConflictTests(unittest.TestCase):
    def test_only_verified_points_inside_explicit_threshold_become_candidates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                store.upsert_spawn_calibration_points("m", [
                    {"spawn_point_index": 0, "x": 0, "y": 0, "z": 0,
                     "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
                    {"spawn_point_index": 1, "x": 3, "y": 4, "z": 0,
                     "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
                    {"spawn_point_index": 2, "x": 100, "y": 0, "z": 0,
                     "validation_status": "EXCLUDED", "heavy_truck_allowed": 0},
                ])
                result = infer_static_point_conflicts(store, "m", "v", 6.0)
                self.assertEqual(2, result["verified_spawn_points"])
                self.assertEqual(1, result["evaluated_pairs"])
                self.assertEqual(1, result["candidate_conflicts"])
                row = store.connection.execute(
                    "SELECT point_a_id, point_b_id, minimum_clearance_m, validation_status "
                    "FROM point_conflicts"
                ).fetchone()
                self.assertEqual(("carla-spawn:0", "carla-spawn:1", 5.0, "STATIC_INFERRED"), row)
                self.assertEqual(
                    {("carla-spawn:0", "carla-spawn:1")},
                    store.static_inferred_conflict_pairs("m", "v"),
                )

    def test_threshold_must_be_positive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                with self.assertRaises(ValueError):
                    infer_static_point_conflicts(store, "m", "v", 0)

    def test_recalculation_removes_candidates_from_an_old_larger_threshold(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                store.upsert_spawn_calibration_points("m", [
                    {"spawn_point_index": 0, "x": 0, "y": 0, "z": 0,
                     "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
                    {"spawn_point_index": 1, "x": 5, "y": 0, "z": 0,
                     "validation_status": "VERIFIED_SPAWN", "heavy_truck_allowed": 1},
                ])
                infer_static_point_conflicts(store, "m", "v", 6.0)
                infer_static_point_conflicts(store, "m", "v", 4.0)
                count = store.connection.execute(
                    "SELECT count(*) FROM point_conflicts "
                    "WHERE conflict_type='STATIC_CENTER_DISTANCE'"
                ).fetchone()[0]
                self.assertEqual(0, count)
