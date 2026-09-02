import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import run_s07_structural_mock


class S07RoadClosureTest(unittest.TestCase):
    def test_only_affected_task_is_replanned_and_road_reopens(self):
        config = load_config(PROJECT_ROOT / "configs" / "s07_road_closure_6v.json")
        result = run_s07_structural_mock(config, seed=202607)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(6, result["initial_assignment_count"])
        self.assertEqual(1, result["replanned_task_count"])
        self.assertEqual(5, result["unaffected_task_count"])
        self.assertEqual(6, result["completed_task_count"])
        self.assertEqual("OPEN", result["road_status_after_reopen"])
        self.assertIn("no_carla_physics", result["simulation_claim"])


if __name__ == "__main__":
    unittest.main()
