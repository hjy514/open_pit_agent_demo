import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import run_s02_structural_mock


class S02VehicleFailureTest(unittest.TestCase):
    def test_failure_releases_and_reassigns_task(self):
        config = load_config(PROJECT_ROOT / "configs" / "s02_vehicle_failure_6v.json")
        result = run_s02_structural_mock(config, seed=202602)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("haul_vehicle_02", result["failed_vehicle_id"])
        self.assertEqual(6, result["initial_assignment_count"])
        self.assertGreaterEqual(len(result["released_task_ids"]), 1)
        self.assertGreaterEqual(result["reassignment_count"], 1)
        self.assertNotIn("haul_vehicle_02", result["reassigned_vehicle_ids"])
        self.assertEqual(6, result["completed_task_count"])
        self.assertIn("no_carla_physics", result["simulation_claim"])


if __name__ == "__main__":
    unittest.main()
