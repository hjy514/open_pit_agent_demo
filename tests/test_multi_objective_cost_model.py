import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_pit_agent.decision import (
    BehaviorCloningPolicy, CandidateCostInput, MapContext,
    MultiObjectiveCostModel, evaluate_behavior_cloning_policy,
    SafetyShield,
    evaluate_global_behavior_cloning_policy, load_cost_weights,
    train_behavior_cloning_policy, train_global_behavior_cloning_policy,
)
from open_pit_agent.models import Position, Task, VehicleState


def vehicle(vehicle_id, available=True, health="healthy", capabilities=None):
    return VehicleState(vehicle_id, vehicle_id, "truck", "haul", "vehicle.cat.cat",
                        capabilities or ["haul"], Position(0, 0), available=available,
                        health=health)


class MultiObjectiveCostModelTests(unittest.TestCase):
    def setUp(self):
        self.weights = load_cost_weights(ROOT / "configs" / "dispatch_cost_v1.json")
        self.model = MultiObjectiveCostModel(self.weights)
        self.task = Task("task-1", "zone-1", 50, ["haul"])

    def test_hard_constraints_remove_invalid_candidates_without_penalty(self):
        blocked = CandidateCostInput(vehicle("blocked"), self.task,
            MapContext(True, 100, ("E1",)), {"closed_edge_ids": ["E1"]})
        failed = CandidateCostInput(vehicle("failed", health="fault"), self.task,
            MapContext(True, 100))
        valid = CandidateCostInput(vehicle("valid"), self.task,
            MapContext(True, 300), metrics={"active_task_count": 0})
        ranked = self.model.rank([blocked, failed, valid])
        self.assertEqual("valid", ranked[0].vehicle_id)
        self.assertTrue(ranked[0].selected)
        self.assertIsNone(next(item for item in ranked if item.vehicle_id == "blocked").total_cost)

    def test_different_units_are_normalized_before_weighting(self):
        short_busy = CandidateCostInput(vehicle("short-busy"), self.task,
            MapContext(True, 100), metrics={"eta_seconds": 20, "active_task_count": 2})
        long_idle = CandidateCostInput(vehicle("long-idle"), self.task,
            MapContext(True, 1000), metrics={"eta_seconds": 200, "active_task_count": 0})
        ranked = self.model.rank([short_busy, long_idle])
        for result in ranked:
            self.assertIn(result.costs["cost_time"]["normalized"], {0.0, 1.0})
            self.assertIn(result.costs["cost_transport"]["normalized"], {0.0, 1.0})

    def test_unavailable_energy_is_not_fabricated_as_zero(self):
        result = self.model.rank([CandidateCostInput(
            vehicle("v1"), self.task, MapContext(True, 100),
            metrics={"nominal_speed_mps": 10, "active_task_count": 0},
        )])[0]
        self.assertEqual("not_available", result.costs["cost_energy"]["availability"])
        self.assertIsNone(result.costs["cost_energy"]["value"])
        self.assertEqual("surrogate_only", result.costs["cost_time"]["availability"])

    def test_red_zone_unknown_route_and_capacity_are_hard_failures(self):
        item = CandidateCostInput(vehicle("v1"), self.task, MapContext(None, None),
            {"target_in_red_zone": True, "red_zone_authorized": False,
             "required_capacity": 100, "vehicle_capacity": 50})
        result = self.model.rank([item])[0]
        failed = {entry["constraint"] for entry in result.constraint_results if not entry["passed"]}
        self.assertEqual({"route_reachable", "route_operational_limit",
                          "red_zone_access", "task_capacity"}, failed)

    def test_safety_shield_rejects_failed_vehicle_and_requires_fallback(self):
        item = CandidateCostInput(
            vehicle("failed", health="fault"), self.task, MapContext(True, 100)
        )
        review = SafetyShield().review(item)
        self.assertEqual("REJECTED", review.status)
        self.assertTrue(review.fallback_required)
        self.assertIn("vehicle health", review.reason)

    def test_safety_shield_requires_explicit_human_confirmation(self):
        pending = CandidateCostInput(
            vehicle("v1"), self.task, MapContext(True, 100),
            {"requires_human_confirmation": True, "human_confirmed": False},
        )
        confirmed = CandidateCostInput(
            vehicle("v1"), self.task, MapContext(True, 100),
            {"requires_human_confirmation": True, "human_confirmed": True},
        )
        self.assertEqual("REJECTED", SafetyShield().review(pending).status)
        self.assertEqual("APPROVED", SafetyShield().review(confirmed).status)

    @staticmethod
    def _bc_candidate(vehicle_id, distance, normalized, feasible=True):
        return {
            "vehicle_id": vehicle_id,
            "feasible": feasible,
            "total_cost": normalized,
            "cost_transport": {"value": distance, "normalized": normalized},
            "cost_workload": {"value": 0.0, "normalized": 0.0},
            "cost_switch": {"value": 0.0, "normalized": 0.0},
        }

    def test_behavior_cloning_learns_feasible_shorter_candidate(self):
        records = []
        for index in range(20):
            candidates = [
                self._bc_candidate("near", 100.0 + index, 0.0),
                self._bc_candidate("far", 500.0 + index, 1.0),
                self._bc_candidate("failed", 1.0, 0.0, feasible=False),
            ]
            records.append({
                "experience_id": "bc-{}".format(index),
                "action": {
                    "action_type": "task_assignment",
                    "selected_vehicle_id": "near",
                    "shadow_policy_evaluation": {
                        "v1_candidate_ranking": candidates,
                    },
                },
            })
        policy, training = train_behavior_cloning_policy(
            records, "dataset-test", epochs=100
        )
        metrics = evaluate_behavior_cloning_policy(policy, records)
        self.assertEqual(20, training["multi_candidate_training_choice_count"])
        self.assertEqual(1.0, metrics["top1_accuracy_multi_candidate"])
        self.assertIn("uniform_chance_accuracy_multi_candidate", metrics)
        self.assertIn("shortest_route_accuracy_multi_candidate", metrics)
        self.assertIn("task_assignment", metrics["action_metrics"])
        decision = policy.decide(candidates)
        self.assertEqual("near", decision["selected_vehicle_id"])
        self.assertNotIn(
            "failed", [item["vehicle_id"] for item in decision["candidate_ranking"]]
        )
        restored = BehaviorCloningPolicy.from_dict(policy.to_dict())
        self.assertEqual("near", restored.decide(candidates)["selected_vehicle_id"])

    def test_global_bc_learns_complete_unique_assignment(self):
        candidate_matrix = {
            "task-a": [
                self._bc_candidate("vehicle-1", 100.0, 0.0),
                self._bc_candidate("vehicle-2", 500.0, 1.0),
            ],
            "task-b": [
                self._bc_candidate("vehicle-1", 200.0, 0.2),
                self._bc_candidate("vehicle-2", 300.0, 0.5),
            ],
        }
        records = [{
            "global_experience_id": "global-{}".format(index),
            "state": {"candidate_matrix": candidate_matrix},
            "action": {"assignments": [
                {"task_id": "task-a", "vehicle_id": "vehicle-1"},
                {"task_id": "task-b", "vehicle_id": "vehicle-2"},
            ]},
            "data_quality": {"eligible_for_global_behavior_cloning": True},
        } for index in range(10)]
        policy, training = train_global_behavior_cloning_policy(
            records, "global-dataset-test", epochs=100
        )
        metrics = evaluate_global_behavior_cloning_policy(policy, records)
        decision = policy.solve(candidate_matrix)
        self.assertEqual(10, training["training_episode_count"])
        self.assertEqual(1.0, metrics["exact_assignment_accuracy"])
        self.assertIn("uniform_chance_exact_accuracy", metrics)
        self.assertIn("shortest_route_exact_accuracy", metrics)
        self.assertIn("multi_objective_min_cost_exact_accuracy", metrics)
        self.assertEqual(1.0, metrics["teacher_equivalent_route_cost_accuracy"])
        self.assertEqual(0.0, metrics["mean_route_cost_regret_m"])
        self.assertEqual(2, decision["feasible_assignment_count"])
        self.assertEqual(
            {"task-a": "vehicle-1", "task-b": "vehicle-2"},
            {item["task_id"]: item["vehicle_id"]
             for item in decision["assignments"]},
        )


if __name__ == "__main__":
    unittest.main()
