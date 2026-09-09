import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.physical_route_validation import (
    STATUS_ENDPOINT_MISMATCH,
    STATUS_REACHED,
    STATUS_STUCK,
    STATUS_TIMEOUT,
    classify_result,
)


class PhysicalRouteValidationTests(unittest.TestCase):
    def test_status_priority_is_deterministic(self):
        self.assertEqual(STATUS_REACHED, classify_result(5.0, 12.0, True, True, True))
        self.assertEqual(STATUS_STUCK, classify_result(20.0, 12.0, True, True, True))
        self.assertEqual(STATUS_ENDPOINT_MISMATCH, classify_result(20.0, 12.0, True, False, True))
        self.assertEqual(STATUS_TIMEOUT, classify_result(20.0, 12.0, True, False, False))

    def test_physical_attempt_is_append_only_and_separate_from_p5(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat", resource_version="v")
                store.upsert_spawn_calibration_points("0325_5", [
                    {"spawn_point_index": 0, "x": 0, "y": 0, "z": 0, "validation_status": "VERIFIED_SPAWN"},
                    {"spawn_point_index": 1, "x": 1, "y": 0, "z": 0, "validation_status": "VERIFIED_SPAWN"},
                ])
                store.start_calibration_run("run-1", "0325_5", "v", "P6", "test", "TEST")
                store.add_route_execution_validation({
                    "validation_id": "attempt-1", "calibration_run_id": "run-1",
                    "map_id": "0325_5", "resource_version": "v", "route_profile_id": "profile",
                    "route_id": "route", "from_point_id": "carla-spawn:0", "to_point_id": "carla-spawn:1",
                    "vehicle_blueprint": "vehicle.cat.cat", "target_speed_kmh": 15,
                    "arrival_tolerance_m": 12, "validation_status": STATUS_REACHED,
                    "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:00:01+00:00",
                    "duration_seconds": 1, "tick_count": 2, "initial_distance_m": 10,
                    "final_distance_m": 5, "distance_travelled_m": 6,
                })
                self.assertIn("route_execution_validations", store.table_names())
                self.assertEqual(1, store.connection.execute(
                    "SELECT count(*) FROM route_execution_validations"
                ).fetchone()[0])
                self.assertEqual(0, store.connection.execute(
                    "SELECT count(*) FROM reachable_pairs"
                ).fetchone()[0])
                records = list(store.physical_route_validations(
                    "0325_5", "v"
                ))
                self.assertEqual(1, len(records))
                self.assertEqual(12.0, records[0]["arrival_tolerance_m"])


if __name__ == "__main__":
    unittest.main()
