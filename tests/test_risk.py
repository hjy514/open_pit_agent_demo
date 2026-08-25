import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.risk import (
    RuleBasedRiskEngine,
    create_risk_review_task,
    load_risk_scenario,
)
from open_pit_agent.scheduler import tasks_from_zones


class RiskBaselineTest(unittest.TestCase):
    def setUp(self):
        self.risk = load_risk_scenario(
            PROJECT_ROOT / "configs" / "risk_slope_synthetic.json"
        )

    def test_synthetic_sequence_escalates_blue_yellow_orange(self):
        engine = RuleBasedRiskEngine(self.risk)

        assessments = [
            engine.assess(observation)
            for observation in self.risk.observations
        ]

        self.assertEqual(
            ["blue", "yellow", "orange"],
            [item.level for item in assessments],
        )
        self.assertEqual("rising", assessments[1].trend)
        self.assertEqual("rising", assessments[2].trend)
        self.assertTrue(assessments[2].synthetic)
        self.assertTrue(assessments[2].reasons)

    def test_orange_assessment_creates_traceable_review_task(self):
        engine = RuleBasedRiskEngine(self.risk)
        assessment = [
            engine.assess(observation)
            for observation in self.risk.observations
        ][-1]

        task = create_risk_review_task(assessment, self.risk.action)

        self.assertEqual("risk_review", task.task_type)
        self.assertEqual(assessment.assessment_id, task.source_event_id)
        self.assertEqual("risk_zone_02", task.zone_id)
        self.assertEqual(200, task.priority)
        self.assertEqual(
            {"inspection", "camera", "lidar"},
            set(task.required_capabilities),
        )

    def test_risk_zone_is_not_an_initial_routine_task(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "town03_risk_response.json"
        )

        tasks = tasks_from_zones(config.zones)

        self.assertEqual(3, len(tasks))
        self.assertNotIn(
            "risk_zone_02",
            {task.zone_id for task in tasks},
        )


if __name__ == "__main__":
    unittest.main()
