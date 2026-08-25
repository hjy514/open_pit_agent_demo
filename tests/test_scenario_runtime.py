import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.scenario_runtime import resolve_scenario


class ScenarioRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(
            PROJECT_ROOT
            / "configs"
            / "town03_fault_recovery.json"
        )

    def test_fixed_seed_resolves_identical_event(self):
        first = resolve_scenario(self.config)
        second = resolve_scenario(self.config)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(202616, first.seed)
        self.assertEqual("fixed", first.mode)
        vehicle_id, tick = first.failure_plan(self.config)
        self.assertEqual("emergency_vehicle_01", vehicle_id)
        self.assertGreaterEqual(tick, 70)
        self.assertLessEqual(tick, 100)

    def test_seed_override_is_recorded(self):
        resolved = resolve_scenario(
            self.config, seed_override=12345
        )

        self.assertEqual(12345, resolved.seed)
        self.assertEqual("seed_override", resolved.mode)
        self.assertEqual(
            "2.0", resolved.to_dict()["profile_version"]
        )
        self.assertIn(
            "temperature_c", resolved.to_dict()["environment"]
        )


if __name__ == "__main__":
    unittest.main()
