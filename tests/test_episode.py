import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import build_episode
from open_pit_agent.scenario_runtime import resolve_scenario


class ConcreteEpisodeTest(unittest.TestCase):
    def test_legacy_three_vehicle_config_becomes_a_fixed_episode(self):
        config = load_config(PROJECT_ROOT / "configs" / "mine_competition_demo.json")

        episode = build_episode(config, run_id="run-regression-001")

        self.assertEqual("run-regression-001", episode.run_id)
        self.assertEqual(3, len(episode.vehicles))
        self.assertEqual(3, episode.fleet_snapshot["total_vehicles"])
        self.assertEqual(2, len(episode.tasks))
        self.assertEqual("inspection_vehicle_01", episode.events[0].target_vehicle_id)
        self.assertEqual(
            "inspection_vehicle_02",
            next(
                item.initial_role
                for item in episode.vehicles
                if item.vehicle_id == "inspection_vehicle_02"
            ),
        )

    def test_seed_reproduces_randomized_fleet_snapshot(self):
        source_path = PROJECT_ROOT / "configs" / "town03.json"
        raw = json.loads(source_path.read_text(encoding="utf-8"))
        template = dict(raw["vehicles"][0])
        while len(raw["vehicles"]) < 6:
            index = len(raw["vehicles"]) + 1
            vehicle = dict(template)
            vehicle["vehicle_id"] = "episode_vehicle_{:02d}".format(index)
            vehicle["role_name"] = "static_role_{:02d}".format(index)
            vehicle["display_name"] = "Episode测试车{:02d}".format(index)
            vehicle["spawn_point_index"] = 40 + index
            raw["vehicles"].append(vehicle)
        raw["demo"]["failure_vehicle_id"] = "episode_vehicle_04"
        raw["fleet"] = {
            "total_vehicles": 6,
            "available_vehicles": 5,
            "active_vehicles": 4,
            "traffic_vehicles": 3,
            "role_policy": "randomized",
            "task_load": "medium",
            "traffic_density": "medium",
            "role_counts": {"production": 4, "inspection": 2},
        }
        with patch("pathlib.Path.read_text", return_value=json.dumps(raw)):
            config = load_config(source_path)

        first = build_episode(config, run_id="run-a", seed=428)
        same_seed = build_episode(config, run_id="run-b", seed=428)
        another_seed = build_episode(config, run_id="run-c", seed=429)

        self.assertEqual(
            [item.initial_role for item in first.vehicles],
            [item.initial_role for item in same_seed.vehicles],
        )
        self.assertEqual(
            [item.available for item in first.vehicles],
            [item.available for item in same_seed.vehicles],
        )
        self.assertNotEqual(
            first.to_dict()["vehicles"], another_seed.to_dict()["vehicles"]
        )

    def test_episode_uses_the_existing_resolved_failure_plan(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "town03_fault_recovery.json"
        )
        resolved = resolve_scenario(config, seed_override=12345)
        expected_vehicle_id, expected_tick = resolved.failure_plan(config)

        episode = build_episode(
            config,
            run_id="resolved-event-001",
            seed=resolved.seed,
            realized_events=resolved.realized_events,
            failure_plan=resolved.failure_plan(config),
        )
        failure = next(
            item for item in episode.events if item.event_type == "vehicle_failure"
        )

        self.assertEqual(expected_vehicle_id, failure.target_vehicle_id)
        self.assertEqual(expected_tick, failure.trigger_tick)
        self.assertEqual("resolved_scenario", failure.parameters["source"])

    def test_normal_episode_has_no_failure_when_explicitly_disabled(self):
        source_path = PROJECT_ROOT / "configs" / "town03.json"
        raw = json.loads(source_path.read_text(encoding="utf-8"))
        raw["demo"].pop("failure_vehicle_id")
        raw["demo"].pop("failure_tick")
        raw["demo"]["failure_enabled"] = False
        with patch("pathlib.Path.read_text", return_value=json.dumps(raw)):
            config = load_config(source_path)

        resolved = resolve_scenario(config)
        episode = build_episode(
            config,
            run_id="normal-no-failure-001",
            seed=resolved.seed,
            realized_events=resolved.realized_events,
            failure_plan=resolved.failure_plan(config),
        )

        self.assertIsNone(resolved.failure_plan(config))
        self.assertEqual([], episode.events)


if __name__ == "__main__":
    unittest.main()
