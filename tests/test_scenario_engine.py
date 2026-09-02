import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.scenario import EventEngine, load_logical_scenario


class ScenarioEngineTest(unittest.TestCase):
    def test_tick_and_state_events_fire_once(self):
        scenario = load_logical_scenario({
            "scenario": {"id": "s01", "version": "1.0", "type": "normal"},
            "events": [
                {"event_id": "e1", "event_type": "TASK_CREATED", "trigger": {"type": "tick", "tick": 2}},
                {"event_id": "e2", "event_type": "VEHICLE_FAILURE", "trigger": {"type": "state", "state_path": "vehicle.v1.health", "state_equals": "failed"}},
            ],
        })
        engine = EventEngine(scenario)
        self.assertEqual([], engine.evaluate(1))
        self.assertEqual(["e1"], [item["event_id"] for item in engine.evaluate(2)])
        self.assertEqual([], engine.evaluate(3, {"vehicle": {"v1": {"health": "healthy"}}}))
        self.assertEqual(["e2"], [item["event_id"] for item in engine.evaluate(4, {"vehicle": {"v1": {"health": "failed"}}})])
        self.assertEqual([], engine.evaluate(5, {"vehicle": {"v1": {"health": "failed"}}}))


if __name__ == "__main__":
    unittest.main()
