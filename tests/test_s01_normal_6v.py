import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import build_episode


class S01NormalSixVehicleTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(PROJECT_ROOT / "configs" / "s01_normal_6v.json")

    def test_s01_has_six_vehicle_master_records_and_tasks(self):
        self.assertEqual(6, len(self.config.vehicles))
        self.assertEqual(6, self.config.fleet.total_vehicles)
        episode = build_episode(self.config, run_id="s01-test", seed=202601)
        self.assertEqual(6, len(episode.vehicles))
        self.assertEqual(6, len(episode.tasks))
        snapshot = episode.to_fleet_snapshot()
        self.assertEqual(6, snapshot.total)
        self.assertEqual(6, len(snapshot.vehicles))

    def test_s01_same_seed_is_reproducible(self):
        first = build_episode(self.config, run_id="run-a", seed=7)
        second = build_episode(self.config, run_id="run-b", seed=7)
        self.assertEqual(first.fleet_snapshot, second.fleet_snapshot)
        self.assertEqual(first.to_fleet_snapshot().to_dict()["vehicles"], second.to_fleet_snapshot().to_dict()["vehicles"])


if __name__ == "__main__":
    unittest.main()
