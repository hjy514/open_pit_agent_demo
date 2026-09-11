import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import load_config
from open_pit_agent.decision_intelligence import (
    build_acceptance_report,
    build_experience_dataset,
    build_risk_guidance,
    load_imitation_memory,
    register_offline_policy_candidate,
)
from open_pit_agent.sqlite_store import SqliteRunStore
from open_pit_agent.risk import (
    RuleBasedRiskEngine,
    load_risk_scenario,
)
from open_pit_agent.scenario_runtime import resolve_scenario


class DecisionIntelligenceTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(
            PROJECT_ROOT / "configs" / "town03_competition_demo.json"
        )
        self.risk = load_risk_scenario(
            PROJECT_ROOT
            / "configs"
            / "risk_slope_competition_synthetic.json"
        )
        self.resolved = resolve_scenario(self.config)

    def test_red_guidance_explains_impact_prevention_and_actions(self):
        assessment = RuleBasedRiskEngine(self.risk).assess(
            self.risk.observations[-1]
        )
        guidance = build_risk_guidance(
            assessment, self.risk, self.resolved
        )

        self.assertEqual("red", guidance["risk_level"])
        self.assertTrue(guidance["impacts"])
        self.assertTrue(guidance["prevention_measures"])
        self.assertEqual(0, len(guidance["planned_actions"]))
        self.assertFalse(guidance["road_restriction_planned"])
        self.assertTrue(
            guidance["human_confirmation_required_for_real_mine"]
        )

    def test_experience_dataset_has_transparent_reward_and_no_fake_training(self):
        summary = {
            "run_id": "run-1",
            "scenario_id": "scenario-1",
            "scenario_seed": 7,
            "scenario_mode": "fixed",
            "mode": "carla-run",
            "risk_assessments": [],
            "tasks": [
                {
                    "task_id": "task-1",
                    "zone_id": "zone-1",
                    "task_type": "inspection",
                    "status": "completed",
                    "assigned_vehicle_id": "vehicle-1",
                }
            ],
        }

        records = build_experience_dataset(summary)

        self.assertEqual(1.0, records[0]["score"]["reward"])
        self.assertTrue(records[0]["eligible_for_imitation"])
        self.assertIn(
            "no_model_trained", records[0]["learning_status"]
        )

    def test_acceptance_report_passes_complete_competition_shape(self):
        summary = {
            "run_id": "run-1",
            "scenario_id": "scenario-1",
            "scenario_seed": 7,
            "mode": "carla-run",
            "failure_injected": True,
            "risk_scenario_id": "risk-1",
            "risk_assessments": [
                {"assessment_id": "a1", "level": "red"}
            ],
            "risk_guidance": [
                {"assessment_id": "a1", "impacts": ["impact"]}
            ],
            "risk_task_ids": ["task-1"],
            "tasks": [{"task_id": "task-1", "status": "completed"}],
            "vehicle_states": [{"speed_mps": 0.0}],
            "work_orders": [{"status": "closed"}],
            "monitoring_dispatch_closed_loop": True,
            "closed_loop_feedback_count": 1,
            "closed_loop_decision_counts": {
                "close_work_order": 1
            },
            "road_restrictions": [{"status": "active"}],
            "route_avoidance_enforced": False,
        }

        report = build_acceptance_report(summary, 1, 1)

        self.assertEqual("PASS", report["overall_status"])
        self.assertEqual(1.0, report["functional_score"])
        self.assertIn("不是矿山工业安全认证", report["scope"])

    def test_acceptance_report_identifies_mine_map_scenario(self):
        report = build_acceptance_report(
            {"scenario_id": "openpit-mine-competition-demo-v1"},
            0,
            0,
        )

        self.assertIn("0325_5露天矿仿真地图", report["scope"])

    def test_takeover_scenario_does_not_require_road_closure(self):
        summary = {
            "run_id": "run-takeover",
            "scenario_id": "openpit-mine-competition-demo-v1",
            "scenario_seed": 7,
            "mode": "carla-run",
            "risk_scenario_id": "risk-1",
            "risk_assessments": [
                {"assessment_id": "a1", "level": "red"}
            ],
            "risk_guidance": [
                {"assessment_id": "a1", "impacts": ["impact"]}
            ],
            "risk_task_ids": [],
            "hazard_information_retained": True,
            "road_restriction_required": False,
            "takeover_completed": True,
            "tasks": [{"task_id": "task-1", "status": "completed"}],
            "vehicle_states": [{"speed_mps": 0.0}],
            "work_orders": [],
            "monitoring_dispatch_closed_loop": True,
            "closed_loop_feedback_count": 0,
        }

        report = build_acceptance_report(summary, 1, 1)

        self.assertEqual("PASS", report["overall_status"])
        names = {item["check_id"] for item in report["checks"]}
        self.assertIn("red_risk_information_retained", names)
        self.assertNotIn("red_risk_restriction_activated", names)

    def test_prior_success_builds_bounded_imitation_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run-1"
            run_dir.mkdir()
            item = {
                "scenario": {"scenario_id": self.config.scenario_id},
                "state": {"task_type": "risk_review"},
                "action": {
                    "assigned_vehicle_id": "inspection_vehicle_02"
                },
                "score": {"reward": 1.0},
                "eligible_for_imitation": True,
            }
            (run_dir / "experience_dataset.jsonl").write_text(
                json.dumps(item) + "\n", encoding="utf-8"
            )

            preferences, metadata = load_imitation_memory(
                Path(temp_dir), self.config.scenario_id
            )

        self.assertGreater(
            preferences[
                ("risk_review", "inspection_vehicle_02")
            ],
            0.0,
        )
        self.assertLessEqual(
            preferences[
                ("risk_review", "inspection_vehicle_02")
            ],
            2.0,
        )
        self.assertFalse(
            metadata["safety_constraints_overridable"]
        )

    def test_offline_candidate_registration_never_promotes_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteRunStore(Path(temp_dir) / "openpit.db")
            try:
                lifecycle = register_offline_policy_candidate(store, {
                    "model_version": "bc-shadow-test",
                    "policy_version": "behavior-cloning-candidate-ranker-v1",
                    "dataset_version": "dataset-test",
                    "status": "TRAINED_OFFLINE_SHADOW_ONLY",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "model_path": "model.json",
                    "training_report_path": "training_report.json",
                    "metrics": {"test": {
                        "evaluable_choice_count": 5,
                        "infeasible_candidate_selected_count": 0,
                        "fallback_count": 0,
                    }},
                }, "candidate")
                row = store.connection.execute(
                    "SELECT status,execution_authority FROM policy_versions "
                    "WHERE model_version='bc-shadow-test'"
                ).fetchone()
            finally:
                store.close()
        self.assertEqual("OFFLINE_EVALUATED_SHADOW_ONLY", lifecycle["status"])
        self.assertEqual(
            ("OFFLINE_EVALUATED_SHADOW_ONLY", "shadow_only"), row
        )
        self.assertEqual("NOT_PROMOTED", lifecycle["promotion_status"])


if __name__ == "__main__":
    unittest.main()
