import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.risk import (
    RuleBasedRiskEngine,
    create_post_action_feedback_observation,
    create_risk_review_task,
    load_risk_scenario,
)
from open_pit_agent.work_order import WorkOrderError, WorkOrderManager


class WorkOrderLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.risk = load_risk_scenario(
            PROJECT_ROOT / "configs" / "risk_slope_synthetic.json"
        )
        engine = RuleBasedRiskEngine(self.risk)
        self.assessment = [
            engine.assess(observation)
            for observation in self.risk.observations
        ][-1]
        self.task = create_risk_review_task(
            self.assessment, self.risk.action
        )

    def test_complete_work_order_lifecycle_and_metrics(self):
        manager = WorkOrderManager()
        order = manager.create_from_risk(
            self.assessment, self.task, self.risk.action, tick=80
        )
        manager.assign(self.task.task_id, "inspection_vehicle_02", tick=80)
        manager.process_adapter_events(
            [
                {
                    "event_type": "task_started",
                    "tick": 80,
                    "payload": {"task_id": self.task.task_id},
                },
                {
                    "event_type": "task_completed",
                    "tick": 644,
                    "payload": {"task_id": self.task.task_id},
                },
            ]
        )

        self.assertEqual([], manager.auto_review(tick=663))
        transitions = manager.auto_review(tick=664)

        self.assertEqual("closed", order.status)
        self.assertEqual(1, len(transitions))
        self.assertEqual(
            order.work_order_id,
            transitions[0].work_order_id,
        )
        self.assertEqual(
            self.task.task_id,
            transitions[0].task_id,
        )
        self.assertEqual(
            "approved_by_demo_rule_no_human_review",
            order.review_result,
        )
        summary = manager.summary()
        self.assertEqual(1.0, summary["work_order_close_rate"])
        self.assertEqual(
            584,
            summary["work_order_metrics"][0]["close_latency_ticks"],
        )

    def test_invalid_transition_is_rejected(self):
        manager = WorkOrderManager()
        manager.create_from_risk(
            self.assessment, self.task, self.risk.action, tick=80
        )

        with self.assertRaises(WorkOrderError):
            manager.process_adapter_events(
                [
                    {
                        "event_type": "task_started",
                        "tick": 80,
                        "payload": {"task_id": self.task.task_id},
                    }
                ]
            )

    def test_task_timeout_escalates_work_order(self):
        manager = WorkOrderManager()
        order = manager.create_from_risk(
            self.assessment, self.task, self.risk.action, tick=80
        )
        manager.assign(self.task.task_id, "inspection_vehicle_02", tick=80)
        manager.process_adapter_events(
            [
                {
                    "event_type": "task_started",
                    "tick": 80,
                    "payload": {"task_id": self.task.task_id},
                },
                {
                    "event_type": "task_timed_out",
                    "tick": 2480,
                    "payload": {"task_id": self.task.task_id},
                },
            ]
        )

        self.assertEqual("escalated", order.status)
        self.assertTrue(manager.all_terminal())
        self.assertEqual(0.0, manager.summary()["work_order_close_rate"])

    def test_safe_feedback_closes_pending_review_order(self):
        manager = WorkOrderManager()
        order = manager.create_from_risk(
            self.assessment, self.task, self.risk.action, tick=80
        )
        manager.assign(self.task.task_id, "inspection_vehicle_02", tick=80)
        manager.process_adapter_events(
            [
                {
                    "event_type": "task_started",
                    "tick": 90,
                    "payload": {"task_id": self.task.task_id},
                },
                {
                    "event_type": "task_completed",
                    "tick": 100,
                    "payload": {"task_id": self.task.task_id},
                },
            ]
        )
        feedback = create_post_action_feedback_observation(
            self.risk,
            tick=120,
            feedback_id="feedback-safe",
        )
        engine = RuleBasedRiskEngine(self.risk)
        feedback_assessment = engine.assess(feedback)

        transition = manager.review_with_feedback(
            self.task.task_id,
            tick=120,
            feedback_level=feedback_assessment.level,
            feedback_id=feedback.sample_id,
        )

        self.assertEqual("blue", feedback_assessment.level)
        self.assertEqual("closed", transition.to_status)
        self.assertEqual("closed", order.status)
        self.assertIn("feedback_blue", order.review_result)

    def test_unsafe_feedback_escalates_pending_review_order(self):
        manager = WorkOrderManager()
        order = manager.create_from_risk(
            self.assessment, self.task, self.risk.action, tick=80
        )
        manager.assign(self.task.task_id, "inspection_vehicle_02", tick=80)
        manager.process_adapter_events(
            [
                {
                    "event_type": "task_started",
                    "tick": 90,
                    "payload": {"task_id": self.task.task_id},
                },
                {
                    "event_type": "task_completed",
                    "tick": 100,
                    "payload": {"task_id": self.task.task_id},
                },
            ]
        )

        transition = manager.review_with_feedback(
            self.task.task_id,
            tick=120,
            feedback_level="red",
            feedback_id="feedback-unsafe",
        )

        self.assertEqual("escalated", transition.to_status)
        self.assertEqual("escalated", order.status)
        self.assertIsNone(order.closed_tick)


if __name__ == "__main__":
    unittest.main()
