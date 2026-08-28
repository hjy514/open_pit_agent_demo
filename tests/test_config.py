import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config


class ConfigTest(unittest.TestCase):
    def test_carla_root_can_be_overridden_for_another_computer(self):
        replacement = "/opt/carla/CARLA_0.9.10"
        with patch.dict(
            "os.environ",
            {"OPENPIT_CARLA_ROOT": replacement},
        ):
            config = load_config(
                PROJECT_ROOT / "configs" / "town03.json"
            )

        self.assertEqual(Path(replacement), config.carla.root)

    def test_town03_config_is_valid_and_heterogeneous(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")

        self.assertEqual("Town03", config.carla.map_name)
        self.assertEqual(3, len(config.vehicles))
        self.assertEqual(3, len(config.zones))
        equipment_types = {item.equipment_type for item in config.vehicles}
        self.assertEqual(3, len(equipment_types))
        self.assertIn(
            "emergency_response",
            next(
                item
                for item in config.vehicles
                if item.vehicle_id == "emergency_vehicle_01"
            ).capabilities,
        )

    def test_competition_config_has_v2_mission_and_failure(self):
        config = load_config(
            PROJECT_ROOT
            / "configs"
            / "town03_competition_demo.json"
        )

        self.assertEqual(
            "2.0",
            config.scenario_variables["profile_version"],
        )
        self.assertTrue(
            config.scenario_variables["disaster"]["active"]
        )
        self.assertEqual(
            "inspection_vehicle_01",
            config.demo.failure_vehicle_id,
        )

    def test_mine_competition_uses_cooked_mine_map(self):
        config = load_config(
            PROJECT_ROOT
            / "configs"
            / "mine_competition_demo.json"
        )

        self.assertEqual("0325_5", config.carla.map_name)
        self.assertEqual(3, len(config.vehicles))
        self.assertGreaterEqual(len(config.zones), 5)
        self.assertEqual(
            [15, 78, 63],
            [item.spawn_point_index for item in config.vehicles],
        )
        self.assertEqual(12.0, config.demo.arrival_tolerance_m)
        self.assertEqual(
            {"vehicle.cat.cat"},
            {vehicle.blueprint for vehicle in config.vehicles},
        )
        self.assertEqual(
            len(config.vehicles),
            len({vehicle.display_name for vehicle in config.vehicles}),
        )
        safe_route = config.scenario_variables[
            "emergency_event"
        ]["safe_route"]
        self.assertEqual(
            "east-slope-safe-bypass-route-01",
            safe_route["route_plan_id"],
        )
        self.assertEqual(15, safe_route["waypoint_spawn_point_index"])


if __name__ == "__main__":
    unittest.main()
