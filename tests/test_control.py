import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.control import (
    ControlError,
    DemoControlManager,
)


class DemoControlTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DemoControlManager(
            project_root=PROJECT_ROOT,
            artifacts_root=Path(self.temp_dir.name) / "runs",
            python_executable="/safe/python",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_red_scenario_builds_fixed_argument_list(self):
        command = self.manager.command_for("red_response")

        self.assertEqual("/safe/python", command[0])
        self.assertEqual(
            str(PROJECT_ROOT / "run_demo.py"), command[1]
        )
        self.assertIn("--load-map", command)
        self.assertIn("--spawn-missing", command)
        self.assertIn("--risk-config", command)
        self.assertIn(
            str(
                PROJECT_ROOT
                / "configs"
                / "risk_slope_red_synthetic.json"
            ),
            command,
        )
        self.assertNotIn("--inject-failure", command)

    def test_unknown_scenario_cannot_build_command(self):
        with self.assertRaises(ControlError):
            self.manager.command_for("../../arbitrary")

    def test_only_allowlisted_fault_scenarios_inject_failure(self):
        fault = self.manager.command_for("fault_recovery")
        competition = self.manager.command_for(
            "competition_demo"
        )
        normal = self.manager.command_for(
            "normal_inspection"
        )

        self.assertIn("--inject-failure", fault)
        self.assertIn("--inject-failure", competition)
        self.assertIn("--risk-config", competition)
        self.assertIn(
            str(
                PROJECT_ROOT
                / "configs"
                / "risk_slope_competition_synthetic.json"
            ),
            competition,
        )
        self.assertNotIn("--inject-failure", normal)


if __name__ == "__main__":
    unittest.main()
