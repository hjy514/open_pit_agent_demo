import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import run_s02_structural_mock, run_structural_scenario


class S02VehicleFailureTest(unittest.TestCase):
    def test_seeded_map_failure_has_hard_rejection_and_shadow_takeover(self):
        config = load_config(PROJECT_ROOT / "configs" / "s02_vehicle_failure_6v.json")
        result = run_structural_scenario(
            "s02", config, seed=202602, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("map_resources_global_p5", result["scenario_source"])
        self.assertEqual(1, result["reassignment_count"])
        self.assertEqual("OPTIMAL", result["policy_comparison"]["optimizer_status"])
        comparison = result["policy_comparison"]["comparisons"][0]
        failed = next(
            item for item in comparison["v1_candidate_ranking"]
            if item["vehicle_id"] == result["failed_vehicle_id"]
        )
        failed_constraints = {
            item["constraint"] for item in failed["constraint_results"]
            if not item["passed"]
        }
        self.assertFalse(failed["feasible"])
        self.assertIn("vehicle_available", failed_constraints)
        self.assertIn("vehicle_healthy", failed_constraints)
        self.assertNotEqual(
            result["failed_vehicle_id"], comparison["shadow_selected_vehicle_v1"]
        )
        self.assertTrue(all(
            draft["planner_version"].startswith("CARLA_GlobalRoutePlanner")
            for draft in result["map_resource_task_draft"]
        ))

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
