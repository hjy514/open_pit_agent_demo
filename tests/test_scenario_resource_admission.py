import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.scenario import admit_scenario_resources


class ScenarioResourceAdmissionTest(unittest.TestCase):
    def _database(self, path):
        store = MapResourceStore(path)
        store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
        store.upsert_spawn_calibration_points("m", [
            {"spawn_point_index": index, "x": index * 10.0, "y": 0.0, "z": 0.0,
             "validation_status": "VERIFIED_SPAWN", "source": "TEST"}
            for index in range(8)
        ])
        store.upsert_point_conflicts([{
            "map_id": "m", "resource_version": "v", "point_a_id": "carla-spawn:0",
            "point_b_id": "carla-spawn:1", "conflict_type": "DUAL_SPAWN",
            "validation_status": "DUAL_SPAWN_BLOCKED", "source": "TEST",
        }])
        store.start_calibration_run("p6", "m", "v", "P6", "test", "TEST")
        store.add_route_execution_validation({
            "validation_id": "reached", "calibration_run_id": "p6", "map_id": "m",
            "resource_version": "v", "route_profile_id": "test", "route_id": "good",
            "from_point_id": "carla-spawn:2", "to_point_id": "carla-spawn:3",
            "vehicle_blueprint": "vehicle.cat.cat", "target_speed_kmh": 10,
            "arrival_tolerance_m": 12, "validation_status": "PHYSICAL_REACHED",
            "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:01:00+00:00",
        })
        store.add_route_execution_validation({
            "validation_id": "stuck", "calibration_run_id": "p6", "map_id": "m",
            "resource_version": "v", "route_profile_id": "test", "route_id": "bad",
            "from_point_id": "carla-spawn:4", "to_point_id": "carla-spawn:5",
            "vehicle_blueprint": "vehicle.cat.cat", "target_speed_kmh": 10,
            "arrival_tolerance_m": 12, "validation_status": "PHYSICAL_STUCK",
            "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:01:00+00:00",
        })
        store.close()

    def test_reproducible_selection_excludes_recorded_blocked_pairs(self):
        config = load_config(PROJECT_ROOT / "configs" / "s01_normal_6v.json")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "resources.db"
            self._database(database)
            config = config.__class__(**dict(
                config.__dict__,
                map_resource=config.map_resource.__class__(database, "m", "v", "R1", {}),
            ))
            first = admit_scenario_resources(config, seed=12)
            second = admit_scenario_resources(config, seed=12)
        self.assertEqual(first.selected_spawn_points, second.selected_spawn_points)
        self.assertEqual(6, len(first.selected_spawn_points))
        ids = {item["point_id"] for item in first.selected_spawn_points}
        self.assertFalse({"carla-spawn:0", "carla-spawn:1"}.issubset(ids))
        self.assertEqual(["good"], [item["route_id"] for item in first.physical_routes_reached])
        self.assertEqual(["bad"], [item["route_id"] for item in first.physical_routes_rejected])


if __name__ == "__main__":
    unittest.main()
