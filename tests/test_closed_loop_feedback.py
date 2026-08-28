import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.cli import _process_closed_loop_feedback
from open_pit_agent.evidence import EvidenceRecorder
from open_pit_agent.risk import (
    RuleBasedRiskEngine,
    create_risk_task,
    load_risk_scenario,
)
from open_pit_agent.work_order import WorkOrderManager


class ClosedLoopFeedbackTest(unittest.TestCase):
    def test_completed_red_risk_task_is_rechecked_and_closed(self):
        scenario = load_risk_scenario(
            PROJECT_ROOT
            / "configs"
            / "risk_slope_competition_synthetic.json"
        )
        engine = RuleBasedRiskEngine(scenario)
        red = engine.assess(scenario.observations[-1])
        task = create_risk_task(red, scenario.action)
        manager = WorkOrderManager()
        order = manager.create_from_risk(
            red, task, scenario.action, tick=520
        )
        manager.assign(task.task_id, "inspection_vehicle_02", tick=520)
        manager.process_adapter_events(
            [
                {
                    "event_type": "task_started",
                    "tick": 540,
                    "payload": {"task_id": task.task_id},
                },
                {
                    "event_type": "task_completed",
                    "tick": 600,
                    "payload": {"task_id": task.task_id},
                },
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(Path(temp_dir), "closed-loop-test")
            transitions, records = _process_closed_loop_feedback(
                manager,
                engine,
                scenario,
                recorder,
                tick=620,
            )
            feedback_path = recorder.run_dir / "feedback_observations.jsonl"
            persisted = [
                json.loads(line)
                for line in feedback_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual("closed", order.status)
        self.assertEqual("closed", transitions[0].to_status)
        self.assertEqual("blue", records[0]["assessment"]["level"])
        self.assertEqual("falling", records[0]["assessment"]["trend"])
        self.assertEqual("close_work_order", records[0]["decision"])
        self.assertEqual(records, persisted)


if __name__ == "__main__":
    unittest.main()
