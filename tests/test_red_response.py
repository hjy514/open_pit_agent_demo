import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.models import Task
from open_pit_agent.restrictions import RestrictionRegistry
from open_pit_agent.risk import (
    RuleBasedRiskEngine,
    actions_for_assessment,
    create_risk_task,
    load_risk_scenario,
)
from open_pit_agent.work_order import WorkOrderManager


class RedResponseTest(unittest.TestCase):
    def setUp(self):
        self.scenario = load_risk_scenario(
            PROJECT_ROOT
            / "configs"
            / "risk_slope_red_synthetic.json"
        )
        engine = RuleBasedRiskEngine(self.scenario)
        self.assessments = [
            engine.assess(observation)
            for observation in self.scenario.observations
        ]
        self.red = self.assessments[-1]

    def test_red_risk_creates_three_distinct_actions(self):
        actions = actions_for_assessment(
            self.scenario, self.red
        )
        tasks = [
            create_risk_task(self.red, action)
            for action in actions
        ]

        self.assertEqual("red", self.red.level)
        self.assertEqual(
            {
                "risk_review",
                "road_control",
                "emergency_response",
            },
            {task.task_type for task in tasks},
        )
        self.assertEqual(3, len({task.task_id for task in tasks}))

    def test_one_risk_event_can_create_distinct_work_orders(self):
        manager = WorkOrderManager()
        orders = []
        for action in actions_for_assessment(
            self.scenario, self.red
        ):
            task = create_risk_task(self.red, action)
            orders.append(
                manager.create_from_risk(
                    self.red,
                    task,
                    action,
                    tick=self.red.tick,
                )
            )

        self.assertEqual(3, len(orders))
        self.assertEqual(
            3, len({order.work_order_id for order in orders})
        )
        self.assertEqual(
            {10, 20},
            {
                order.auto_review_delay_ticks
                for order in orders
            },
        )

    def test_red_restriction_cancels_ordinary_zone_task(self):
        registry = RestrictionRegistry()
        restriction = registry.activate(
            self.red,
            self.scenario.restriction,
            tick=self.red.tick,
        )
        ordinary = Task(
            task_id="ordinary-entry",
            zone_id="risk_zone_02",
            priority=10,
            required_capabilities=["inspection"],
            task_type="routine_inspection",
        )
        emergency = Task(
            task_id="authorized-entry",
            zone_id="risk_zone_02",
            priority=300,
            required_capabilities=["inspection"],
            task_type="emergency_response",
        )

        cancelled = registry.apply_to_tasks(
            [ordinary, emergency]
        )

        self.assertEqual(
            "town03_proxy_risk_segment_02",
            restriction.road_segment_id,
        )
        self.assertEqual(["ordinary-entry"], cancelled)
        self.assertEqual("cancelled", ordinary.status)
        self.assertEqual("pending", emergency.status)
        self.assertFalse(
            registry.summary()["route_avoidance_enforced"]
        )

        registry.mark_route_avoidance_enforced(
            "safe-route-01",
            "carla_basic_agent_via_safe_waypoint",
        )
        self.assertTrue(
            registry.summary()["route_avoidance_enforced"]
        )
        self.assertEqual(
            "safe-route-01",
            registry.summary()["safe_route_plan_id"],
        )

        deactivated = registry.deactivate_all(
            tick=self.red.tick + 100,
            reason="all_feedback_reviews_safe",
        )

        self.assertEqual(1, len(deactivated))
        self.assertEqual("inactive", restriction.status)
        self.assertEqual(
            0,
            registry.summary()["active_road_restriction_count"],
        )
        self.assertEqual(
            "all_feedback_reviews_safe",
            restriction.deactivation_reason,
        )


if __name__ == "__main__":
    unittest.main()
