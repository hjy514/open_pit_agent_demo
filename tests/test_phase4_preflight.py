import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore

SPEC = importlib.util.spec_from_file_location(
    "check_phase4_preflight", str(PROJECT_ROOT / "scripts" / "check_phase4_preflight.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Phase4PreflightTests(unittest.TestCase):
    def test_map_name_matching_and_resource_counts(self):
        self.assertTrue(MODULE.map_matches("/Game/Carla/Maps/0325_5", "0325_5"))
        self.assertFalse(MODULE.map_matches("Town03", "0325_5"))
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                store.upsert_spawn_calibration_points("m", [{
                    "spawn_point_index": 0, "x": 0, "y": 0, "z": 0,
                    "heavy_truck_allowed": 1, "validation_status": "VERIFIED_SPAWN",
                }, {
                    "spawn_point_index": 1, "x": 1, "y": 0, "z": 0,
                    "heavy_truck_allowed": 1, "validation_status": "VERIFIED_SPAWN",
                }])
                store.upsert_point_conflicts([{
                    "map_id": "m", "resource_version": "v", "point_a_id": "carla-spawn:0",
                    "point_b_id": "carla-spawn:1", "conflict_type": "STATIC_CENTER_DISTANCE",
                    "validation_status": "STATIC_INFERRED", "source": "TEST",
                }])
                self.assertEqual(
                    {"verified_spawn_points": 2, "static_candidate_conflicts": 1},
                    MODULE.resource_counts(store, "m", "v"),
                )
                store.upsert_point_conflicts([{
                    "map_id": "m", "resource_version": "v",
                    "point_a_id": "carla-spawn:0", "point_b_id": "carla-spawn:1",
                    "conflict_type": "DUAL_HEAVY_TRUCK_SPAWN_CHECK",
                    "validation_status": "DUAL_SPAWN_VERIFIED", "source": "TEST",
                }])
                self.assertEqual(
                    {"verified_spawn_points": 2, "static_candidate_conflicts": 0},
                    MODULE.resource_counts(store, "m", "v"),
                )
