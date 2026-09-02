import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import run_s01_structural_mock


class S01StructuralRunnerTest(unittest.TestCase):
    def test_runner_completes_all_configured_tasks_without_carla_claim(self):
        config = load_config(PROJECT_ROOT / "configs" / "s01_normal_6v.json")
        result = run_s01_structural_mock(config, seed=11)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("mock_structural", result["mode"])
        self.assertEqual(6, result["fleet"]["total"])
        self.assertEqual(6, result["assignment_count"])
        self.assertEqual(6, result["completed_task_count"])
        self.assertIn("no_carla_physics", result["simulation_claim"])


if __name__ == "__main__":
    unittest.main()
