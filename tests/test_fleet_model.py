import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.scenario import VehicleMaster, resolve_fleet, snapshot_from_episode


class LegacyVehicle:
    vehicle_id = "v1"
    display_name = "V1"
    equipment_type = "truck"
    blueprint = "b"
    capabilities = ["haul"]
    initial_role = "production"
    initial_status = "active"
    spawn_point_index = 3
    available = True
    active = True
    in_traffic = False


class LegacyEpisode:
    seed = 9
    fleet_snapshot = {"total_vehicles": 1, "available_vehicles": 1, "active_vehicles": 1, "traffic_vehicles": 0}
    vehicles = [LegacyVehicle()]


class FleetModelTest(unittest.TestCase):
    def test_snapshot_is_reproducible_and_master_is_role_neutral(self):
        masters = [
            VehicleMaster("v1", "V1", "truck", "vehicle.cat.cat", ["haul"]),
            VehicleMaster("v2", "V2", "truck", "vehicle.cat.cat", ["haul"]),
            VehicleMaster("v3", "V3", "support", "vehicle.cat.cat", ["inspect"]),
        ]
        first = resolve_fleet(masters, 3, 2, 2, 1, seed=7)
        second = resolve_fleet(masters, 3, 2, 2, 1, seed=7)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(["unassigned"] * 3, [item.role for item in first.vehicles])
        self.assertEqual(2, sum(item.available for item in first.vehicles))

    def test_legacy_episode_can_be_converted_without_mutation(self):
        snapshot = snapshot_from_episode(LegacyEpisode())
        self.assertEqual(1, snapshot.total)
        self.assertEqual("production", snapshot.vehicles[0].role)
        self.assertEqual({"total_vehicles": 1, "available_vehicles": 1, "active_vehicles": 1, "traffic_vehicles": 0}, LegacyEpisode.fleet_snapshot)

    def test_invalid_count_relationship_is_rejected(self):
        master = [VehicleMaster("v1", "V1", "truck", "b", [])]
        with self.assertRaises(ValueError):
            resolve_fleet(master, 1, 1, 2, 0, seed=1)


if __name__ == "__main__":
    unittest.main()
