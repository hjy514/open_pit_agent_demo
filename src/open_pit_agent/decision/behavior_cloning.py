"""Dependency-free behavior-cloning candidate ranker for structural data.

The model is a conditional softmax linear ranker.  It learns only among
hard-constraint-feasible candidates; infeasible candidates never enter the
policy choice set.  CARLA execution and policy promotion are out of scope.
"""
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


MODEL_TYPE = "DispatchPolicy"
POLICY_VERSION = "behavior-cloning-candidate-ranker-v1"
GLOBAL_POLICY_VERSION = "behavior-cloning-global-assignment-v2"
FEATURE_NAMES = (
    "route_length_m",
    "transport_cost_normalized",
    "workload_cost_normalized",
    "switch_cost",
    "multi_objective_total_cost",
)
SUPPORTED_ACTIONS = (
    "task_assignment",
    "task_reassignment_after_vehicle_failure",
)


def _candidate_features(candidate: Dict[str, Any]) -> Optional[List[float]]:
    transport = candidate.get("cost_transport", {})
    workload = candidate.get("cost_workload", {})
    switching = candidate.get("cost_switch", {})
    values = (
        transport.get("value"),
        transport.get("normalized"),
        workload.get("normalized"),
        switching.get("value"),
        candidate.get("total_cost"),
    )
    if any(value is None for value in values):
        return None
    return [float(value) for value in values]


def _choice_set(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = record.get("action", {})
    if action.get("action_type") not in SUPPORTED_ACTIONS:
        return None
    comparison = action.get("shadow_policy_evaluation") or {}
    candidates = []
    for item in comparison.get("v1_candidate_ranking", []):
        if not isinstance(item, dict) or not item.get("feasible"):
            continue
        features = _candidate_features(item)
        if features is None:
            continue
        candidates.append({
            "vehicle_id": str(item.get("vehicle_id")),
            "features": features,
            "source": item,
        })
    selected_vehicle_id = action.get("selected_vehicle_id")
    if not candidates or selected_vehicle_id not in {
        item["vehicle_id"] for item in candidates
    }:
        return None
    return {
        "experience_id": record.get("experience_id"),
        "action_type": str(action.get("action_type")),
        "selected_vehicle_id": str(selected_vehicle_id),
        "candidates": candidates,
    }


def load_dataset_split(path: Path) -> List[Dict[str, Any]]:
    records = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


@dataclass
class BehaviorCloningPolicy:
    feature_names: Tuple[str, ...]
    feature_means: List[float]
    feature_scales: List[float]
    weights: List[float]
    dataset_version: str
    policy_version: str = POLICY_VERSION

    def _normalize(self, features: Sequence[float]) -> List[float]:
        return [
            (float(value) - self.feature_means[index]) / self.feature_scales[index]
            for index, value in enumerate(features)
        ]

    def score_candidate(self, candidate: Dict[str, Any]) -> Optional[float]:
        if not candidate.get("feasible"):
            return None
        features = _candidate_features(candidate)
        if features is None:
            return None
        normalized = self._normalize(features)
        return sum(weight * value for weight, value in zip(self.weights, normalized))

    def rank(self, candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rank only feasible, fully observed candidates; higher is better."""
        ranked = []
        for candidate in candidates:
            score = self.score_candidate(candidate)
            if score is None:
                continue
            ranked.append({
                "vehicle_id": str(candidate.get("vehicle_id")),
                "bc_score": round(float(score), 8),
                "feasible": True,
                "policy_version": self.policy_version,
            })
        ranked.sort(key=lambda item: (-item["bc_score"], item["vehicle_id"]))
        return ranked

    def decide(self, candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        ranked = self.rank(candidates)
        if not ranked:
            return {
                "status": "FALLBACK_REQUIRED",
                "selected_vehicle_id": None,
                "reason": "no_feasible_fully_observed_candidate",
                "policy_version": self.policy_version,
                "candidate_ranking": [],
            }
        return {
            "status": "SHADOW_DECISION_READY",
            "selected_vehicle_id": ranked[0]["vehicle_id"],
            "reason": "highest_bc_candidate_score_after_hard_constraints",
            "policy_version": self.policy_version,
            "candidate_ranking": ranked,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_type": MODEL_TYPE,
            "policy_version": self.policy_version,
            "dataset_version": self.dataset_version,
            "feature_names": list(self.feature_names),
            "feature_means": self.feature_means,
            "feature_scales": self.feature_scales,
            "weights": self.weights,
            "hard_constraints_applied_before_policy": True,
            "execution_authority": "shadow_only_no_carla_command",
            "fallback_policy": "HeuristicBaselineScheduler",
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BehaviorCloningPolicy":
        return cls(
            feature_names=tuple(payload["feature_names"]),
            feature_means=[float(item) for item in payload["feature_means"]],
            feature_scales=[float(item) for item in payload["feature_scales"]],
            weights=[float(item) for item in payload["weights"]],
            dataset_version=str(payload["dataset_version"]),
            policy_version=str(payload.get("policy_version", POLICY_VERSION)),
        )


@dataclass
class GlobalBehaviorCloningPolicy(BehaviorCloningPolicy):
    policy_version: str = GLOBAL_POLICY_VERSION

    def solve(self, candidate_matrix: Dict[str, Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
        prepared = _prepare_global_candidates(candidate_matrix)
        assignments = _enumerate_global_assignments(prepared)
        if not assignments:
            return {
                "status": "FALLBACK_REQUIRED", "assignments": [],
                "reason": "no_complete_unique_feasible_assignment",
                "policy_version": self.policy_version,
            }
        scored = []
        for mapping, feature_sum in assignments:
            normalized_sum = [
                (feature_sum[index] - len(mapping) * self.feature_means[index])
                / self.feature_scales[index]
                for index in range(len(self.feature_names))
            ]
            score = sum(
                weight * value for weight, value in zip(self.weights, normalized_sum)
            )
            scored.append((float(score), mapping))
        scored.sort(key=lambda item: (-item[0], sorted(item[1].items())))
        return {
            "status": "SHADOW_DECISION_READY",
            "assignments": [
                {"task_id": task_id, "vehicle_id": vehicle_id}
                for task_id, vehicle_id in sorted(scored[0][1].items())
            ],
            "global_bc_score": round(scored[0][0], 8),
            "feasible_assignment_count": len(scored),
            "reason": "maximum_global_bc_score_with_unique_vehicle_constraint",
            "policy_version": self.policy_version,
        }


def _prepare_global_candidates(
    candidate_matrix: Dict[str, Sequence[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    prepared = {}
    for task_id, candidates in candidate_matrix.items():
        values = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("feasible"):
                continue
            features = _candidate_features(candidate)
            if features is None:
                continue
            values.append({
                "vehicle_id": str(candidate.get("vehicle_id")),
                "features": features,
                "source": candidate,
            })
        if values:
            prepared[str(task_id)] = values
    return prepared


def _enumerate_global_assignments(
    prepared: Dict[str, List[Dict[str, Any]]]
) -> List[Tuple[Dict[str, str], List[float]]]:
    tasks = sorted(prepared, key=lambda task_id: (len(prepared[task_id]), task_id))
    if not tasks:
        return []
    output = []

    def search(index, used, mapping, feature_sum):
        if index == len(tasks):
            output.append((dict(mapping), list(feature_sum)))
            return
        task_id = tasks[index]
        for candidate in prepared[task_id]:
            vehicle_id = candidate["vehicle_id"]
            if vehicle_id in used:
                continue
            mapping[task_id] = vehicle_id
            search(
                index + 1, used | {vehicle_id}, mapping,
                [left + right for left, right in zip(feature_sum, candidate["features"])],
            )
            mapping.pop(task_id)

    search(0, set(), {}, [0.0] * len(FEATURE_NAMES))
    return output


def _global_examples(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    examples = []
    for record in records:
        if not record.get("data_quality", {}).get(
            "eligible_for_global_behavior_cloning", False
        ):
            continue
        prepared = _prepare_global_candidates(
            record.get("state", {}).get("candidate_matrix", {})
        )
        assignments = _enumerate_global_assignments(prepared)
        teacher = {
            str(item["task_id"]): str(item["vehicle_id"])
            for item in record.get("action", {}).get("assignments", [])
        }
        expert = next(
            (features for mapping, features in assignments if mapping == teacher), None
        )
        if expert is None or len(assignments) < 2:
            continue
        examples.append({
            "global_experience_id": record.get("global_experience_id"),
            "prepared": prepared,
            "assignments": assignments,
            "teacher": teacher,
            "expert_features": expert,
        })
    return examples


def train_global_behavior_cloning_policy(
    records: Sequence[Dict[str, Any]],
    dataset_version: str,
    epochs: int = 400,
    learning_rate: float = 0.05,
    l2: float = 0.0001,
) -> Tuple[GlobalBehaviorCloningPolicy, Dict[str, Any]]:
    examples = _global_examples(records)
    if not examples:
        raise ValueError("no learnable global assignment episodes are available")
    candidate_features = [
        candidate["features"] for example in examples
        for candidates in example["prepared"].values() for candidate in candidates
    ]
    means = [
        sum(row[index] for row in candidate_features) / len(candidate_features)
        for index in range(len(FEATURE_NAMES))
    ]
    scales = []
    for index, mean in enumerate(means):
        variance = sum(
            (row[index] - mean) ** 2 for row in candidate_features
        ) / len(candidate_features)
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    weights = [0.0] * len(FEATURE_NAMES)
    loss_history = []
    for epoch in range(int(epochs)):
        gradient = [0.0] * len(weights)
        loss = 0.0
        for example in examples:
            task_count = len(example["teacher"])
            normalized_assignments = [[
                (value - task_count * means[index]) / scales[index]
                for index, value in enumerate(feature_sum)
            ] for _, feature_sum in example["assignments"]]
            logits = [
                sum(weight * value for weight, value in zip(weights, features))
                for features in normalized_assignments
            ]
            probabilities = _softmax(logits)
            expert_index = next(
                index for index, (mapping, _) in enumerate(example["assignments"])
                if mapping == example["teacher"]
            )
            loss -= math.log(max(probabilities[expert_index], 1e-12))
            for feature_index in range(len(weights)):
                expected = sum(
                    probability * features[feature_index]
                    for probability, features in zip(probabilities, normalized_assignments)
                )
                gradient[feature_index] += (
                    expected - normalized_assignments[expert_index][feature_index]
                )
        count = float(len(examples))
        for index in range(len(weights)):
            weights[index] -= float(learning_rate) * (
                gradient[index] / count + float(l2) * weights[index]
            )
        if epoch in (0, int(epochs) - 1):
            loss_history.append({
                "epoch": epoch + 1,
                "mean_cross_entropy": round(loss / count, 8),
            })
    policy = GlobalBehaviorCloningPolicy(
        feature_names=FEATURE_NAMES,
        feature_means=means,
        feature_scales=scales,
        weights=weights,
        dataset_version=str(dataset_version),
    )
    return policy, {
        "training_episode_count": len(examples),
        "epochs": int(epochs),
        "learning_rate": float(learning_rate),
        "l2": float(l2),
        "loss_history": loss_history,
    }


def evaluate_global_behavior_cloning_policy(
    policy: GlobalBehaviorCloningPolicy,
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    examples = _global_examples(records)
    eligible_episode_count = sum(
        bool(record.get("data_quality", {}).get(
            "eligible_for_global_behavior_cloning", False
        ))
        for record in records
    )
    exact = 0
    correct_tasks = 0
    task_count = 0
    fallback = 0
    assignment_space = []
    chance_exact_sum = 0.0
    shortest_exact = 0
    shortest_correct_tasks = 0
    minimum_cost_exact = 0
    minimum_cost_correct_tasks = 0
    route_cost_equivalent = 0
    route_cost_regrets = []
    for example in examples:
        decision = policy.solve({
            task_id: [item["source"] for item in candidates]
            for task_id, candidates in example["prepared"].items()
        })
        if decision["status"] != "SHADOW_DECISION_READY":
            fallback += 1
            continue
        predicted = {
            item["task_id"]: item["vehicle_id"] for item in decision["assignments"]
        }
        expert = example["teacher"]
        exact += int(predicted == expert)
        correct_tasks += sum(predicted.get(task_id) == vehicle_id
                             for task_id, vehicle_id in expert.items())
        task_count += len(expert)
        assignment_space.append(decision["feasible_assignment_count"])
        chance_exact_sum += 1.0 / len(example["assignments"])

        source_by_pair = {
            (task_id, candidate["vehicle_id"]): candidate["source"]
            for task_id, candidates in example["prepared"].items()
            for candidate in candidates
        }

        def assignment_cost(mapping, field):
            if field == "route_length":
                return sum(
                    float(source_by_pair[(task_id, vehicle_id)]
                          ["cost_transport"]["value"])
                    for task_id, vehicle_id in mapping.items()
                )
            return sum(
                float(source_by_pair[(task_id, vehicle_id)]["total_cost"])
                for task_id, vehicle_id in mapping.items()
            )

        predicted_route_cost = assignment_cost(predicted, "route_length")
        teacher_route_cost = assignment_cost(expert, "route_length")
        route_regret = predicted_route_cost - teacher_route_cost
        route_cost_regrets.append(route_regret)
        route_cost_equivalent += int(
            abs(route_regret) <= max(1e-6, abs(teacher_route_cost) * 1e-9)
        )

        shortest = min(
            (mapping for mapping, _ in example["assignments"]),
            key=lambda mapping: (
                assignment_cost(mapping, "route_length"),
                sorted(mapping.items()),
            ),
        )
        minimum_cost = min(
            (mapping for mapping, _ in example["assignments"]),
            key=lambda mapping: (
                assignment_cost(mapping, "multi_objective"),
                sorted(mapping.items()),
            ),
        )
        shortest_exact += int(shortest == expert)
        shortest_correct_tasks += sum(
            shortest.get(task_id) == vehicle_id
            for task_id, vehicle_id in expert.items()
        )
        minimum_cost_exact += int(minimum_cost == expert)
        minimum_cost_correct_tasks += sum(
            minimum_cost.get(task_id) == vehicle_id
            for task_id, vehicle_id in expert.items()
        )
    return {
        "policy_version": policy.policy_version,
        "input_episode_count": len(records),
        "eligible_episode_count": eligible_episode_count,
        "episode_count": len(examples),
        "non_learnable_single_assignment_episode_count": (
            eligible_episode_count - len(examples)
        ),
        "exact_assignment_accuracy": round(exact / len(examples), 6) if examples else None,
        "per_task_assignment_accuracy": round(correct_tasks / task_count, 6)
        if task_count else None,
        "teacher_equivalent_route_cost_accuracy": round(
            route_cost_equivalent / len(examples), 6
        ) if examples else None,
        "mean_route_cost_regret_m": round(
            sum(route_cost_regrets) / len(route_cost_regrets), 9
        ) if route_cost_regrets else None,
        "maximum_route_cost_regret_m": round(
            max(route_cost_regrets), 9
        ) if route_cost_regrets else None,
        "uniform_chance_exact_accuracy": round(
            chance_exact_sum / len(examples), 6
        ) if examples else None,
        "shortest_route_exact_accuracy": round(
            shortest_exact / len(examples), 6
        ) if examples else None,
        "shortest_route_per_task_accuracy": round(
            shortest_correct_tasks / task_count, 6
        ) if task_count else None,
        "multi_objective_min_cost_exact_accuracy": round(
            minimum_cost_exact / len(examples), 6
        ) if examples else None,
        "multi_objective_min_cost_per_task_accuracy": round(
            minimum_cost_correct_tasks / task_count, 6
        ) if task_count else None,
        "fallback_count": fallback,
        "infeasible_candidate_selected_count": 0,
        "mean_feasible_assignment_count": round(
            sum(assignment_space) / len(assignment_space), 6
        ) if assignment_space else None,
        "scope": "offline_s01_global_assignment_imitation_only",
    }


def _softmax(values: Sequence[float]) -> List[float]:
    maximum = max(values)
    exponentials = [math.exp(value - maximum) for value in values]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def train_behavior_cloning_policy(
    records: Sequence[Dict[str, Any]],
    dataset_version: str,
    epochs: int = 400,
    learning_rate: float = 0.05,
    l2: float = 0.0001,
) -> Tuple[BehaviorCloningPolicy, Dict[str, Any]]:
    choices = [item for item in (_choice_set(record) for record in records) if item]
    learnable = [item for item in choices if len(item["candidates"]) >= 2]
    if not learnable:
        raise ValueError("no multi-candidate behavior-cloning choices are available")
    all_features = [
        candidate["features"] for choice in learnable for candidate in choice["candidates"]
    ]
    means = [
        sum(row[index] for row in all_features) / len(all_features)
        for index in range(len(FEATURE_NAMES))
    ]
    scales = []
    for index, mean in enumerate(means):
        variance = sum(
            (row[index] - mean) ** 2 for row in all_features
        ) / len(all_features)
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    weights = [0.0] * len(FEATURE_NAMES)
    loss_history = []
    for epoch in range(int(epochs)):
        gradient = [0.0] * len(weights)
        loss = 0.0
        for choice in learnable:
            normalized = [[
                (value - means[index]) / scales[index]
                for index, value in enumerate(candidate["features"])
            ] for candidate in choice["candidates"]]
            logits = [
                sum(weight * value for weight, value in zip(weights, row))
                for row in normalized
            ]
            probabilities = _softmax(logits)
            selected_index = next(
                index for index, candidate in enumerate(choice["candidates"])
                if candidate["vehicle_id"] == choice["selected_vehicle_id"]
            )
            loss -= math.log(max(probabilities[selected_index], 1e-12))
            for feature_index in range(len(weights)):
                expected = sum(
                    probability * row[feature_index]
                    for probability, row in zip(probabilities, normalized)
                )
                gradient[feature_index] += (
                    expected - normalized[selected_index][feature_index]
                )
        count = float(len(learnable))
        for index in range(len(weights)):
            gradient[index] = gradient[index] / count + l2 * weights[index]
            weights[index] -= float(learning_rate) * gradient[index]
        if epoch in (0, int(epochs) - 1):
            loss_history.append({
                "epoch": epoch + 1,
                "mean_cross_entropy": round(loss / count, 8),
            })
    policy = BehaviorCloningPolicy(
        feature_names=FEATURE_NAMES,
        feature_means=means,
        feature_scales=scales,
        weights=weights,
        dataset_version=str(dataset_version),
    )
    return policy, {
        "training_record_count": len(records),
        "valid_choice_count": len(choices),
        "multi_candidate_training_choice_count": len(learnable),
        "single_candidate_excluded_from_gradient_count": len(choices) - len(learnable),
        "epochs": int(epochs),
        "learning_rate": float(learning_rate),
        "l2": float(l2),
        "loss_history": loss_history,
    }


def evaluate_behavior_cloning_policy(
    policy: BehaviorCloningPolicy,
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    choices = [item for item in (_choice_set(record) for record in records) if item]
    multi = 0
    correct = 0
    multi_correct = 0
    reciprocal_rank_sum = 0.0
    fallback_count = 0
    chance_accuracy_sum = 0.0
    shortest_route_correct = 0
    multi_objective_correct = 0
    action_breakdown: Dict[str, Dict[str, int]] = {}
    for choice in choices:
        ranked = policy.rank([item["source"] for item in choice["candidates"]])
        if not ranked:
            fallback_count += 1
            continue
        ids = [item["vehicle_id"] for item in ranked]
        selected = choice["selected_vehicle_id"]
        is_correct = ids[0] == selected
        correct += int(is_correct)
        rank = ids.index(selected) + 1
        reciprocal_rank_sum += 1.0 / rank
        if len(ids) >= 2:
            multi += 1
            multi_correct += int(is_correct)
            chance_accuracy_sum += 1.0 / len(ids)
            source_by_id = {
                item["vehicle_id"]: item["source"] for item in choice["candidates"]
            }
            shortest = min(
                ids,
                key=lambda vehicle_id: (
                    float(source_by_id[vehicle_id]["cost_transport"]["value"]),
                    vehicle_id,
                ),
            )
            minimum_cost = min(
                ids,
                key=lambda vehicle_id: (
                    float(source_by_id[vehicle_id]["total_cost"]), vehicle_id
                ),
            )
            shortest_route_correct += int(shortest == selected)
            multi_objective_correct += int(minimum_cost == selected)
        action_type = choice["action_type"]
        bucket = action_breakdown.setdefault(
            action_type, {"choices": 0, "correct": 0, "multi_choices": 0,
                           "multi_correct": 0}
        )
        bucket["choices"] += 1
        bucket["correct"] += int(is_correct)
        if len(ids) >= 2:
            bucket["multi_choices"] += 1
            bucket["multi_correct"] += int(is_correct)
    action_metrics = {}
    for action_type, bucket in sorted(action_breakdown.items()):
        action_metrics[action_type] = dict(bucket)
        action_metrics[action_type]["top1_accuracy"] = round(
            bucket["correct"] / bucket["choices"], 6
        ) if bucket["choices"] else None
        action_metrics[action_type]["multi_candidate_top1_accuracy"] = round(
            bucket["multi_correct"] / bucket["multi_choices"], 6
        ) if bucket["multi_choices"] else None
    return {
        "policy_version": policy.policy_version,
        "record_count": len(records),
        "evaluable_choice_count": len(choices),
        "multi_candidate_choice_count": multi,
        "top1_accuracy_all_choices": (
            round(correct / len(choices), 6) if choices else None
        ),
        "top1_accuracy_multi_candidate": (
            round(multi_correct / multi, 6) if multi else None
        ),
        "uniform_chance_accuracy_multi_candidate": (
            round(chance_accuracy_sum / multi, 6) if multi else None
        ),
        "shortest_route_accuracy_multi_candidate": (
            round(shortest_route_correct / multi, 6) if multi else None
        ),
        "multi_objective_min_cost_accuracy_multi_candidate": (
            round(multi_objective_correct / multi, 6) if multi else None
        ),
        "mean_reciprocal_rank": (
            round(reciprocal_rank_sum / len(choices), 6) if choices else None
        ),
        "fallback_count": fallback_count,
        "infeasible_candidate_selected_count": 0,
        "action_metrics": action_metrics,
        "scope": "offline_teacher_imitation_only_no_carla_execution",
    }


def train_and_save_behavior_cloning_policy(
    dataset_manifest_path: Path,
    models_root: Path,
    model_version: str,
    epochs: int = 400,
) -> Dict[str, Any]:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", str(model_version)):
        raise ValueError("model version must use 1-80 safe filename characters")
    manifest = json.loads(Path(dataset_manifest_path).read_text(encoding="utf-8"))
    if not str(manifest.get("status", "")).startswith("DATASET_VERSION_READY"):
        raise ValueError("dataset version is not ready")
    manifest_dir = Path(dataset_manifest_path).parent

    def split_path(name):
        configured = Path(manifest["splits"][name]["path"])
        return configured if configured.is_file() else manifest_dir / "{}.jsonl".format(name)

    train_records = load_dataset_split(split_path("train"))
    validation_records = load_dataset_split(split_path("validation"))
    test_records = load_dataset_split(split_path("test"))
    policy, training = train_behavior_cloning_policy(
        train_records, manifest["dataset_version"], epochs=epochs
    )
    output_dir = Path(models_root) / "dispatch_policy" / str(model_version)
    output_dir.mkdir(parents=True, exist_ok=False)
    model_path = output_dir / "model.json"
    model_path.write_text(
        json.dumps(policy.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = {
        "model_type": MODEL_TYPE,
        "model_version": str(model_version),
        "policy_version": policy.policy_version,
        "status": "TRAINED_OFFLINE_SHADOW_ONLY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": manifest["dataset_version"],
        "dataset_manifest_path": str(Path(dataset_manifest_path)),
        "model_path": str(model_path),
        "training": training,
        "metrics": {
            "train": evaluate_behavior_cloning_policy(policy, train_records),
            "validation": evaluate_behavior_cloning_policy(policy, validation_records),
            "test": evaluate_behavior_cloning_policy(policy, test_records),
        },
        "promotion_status": "NOT_PROMOTED",
        "execution_authority": "shadow_only",
        "safety_requirements": [
            "hard_constraints_before_ranking",
            "safety_shield_before_execution",
            "fallback_to_heuristic_baseline_on_invalid_output",
            "carla_ab_validation_required_before_promotion",
        ],
    }
    report_path = output_dir / "training_report.json"
    report["training_report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def train_and_save_global_behavior_cloning_policy(
    dataset_manifest_path: Path,
    models_root: Path,
    model_version: str,
    epochs: int = 400,
) -> Dict[str, Any]:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", str(model_version)):
        raise ValueError("model version must use 1-80 safe filename characters")
    manifest_path = Path(dataset_manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not str(manifest.get("status", "")).startswith("DATASET_VERSION_READY"):
        raise ValueError("dataset version is not ready")
    global_view = manifest.get("global_assignment_view")
    if not isinstance(global_view, dict):
        raise ValueError("dataset has no global assignment V2 view")

    def global_split_path(name):
        configured = Path(global_view["splits"][name]["path"])
        fallback = manifest_path.parent / "global_{}.jsonl".format(name)
        return configured if configured.is_file() else fallback

    train_records = load_dataset_split(global_split_path("train"))
    validation_records = load_dataset_split(global_split_path("validation"))
    test_records = load_dataset_split(global_split_path("test"))
    policy, training = train_global_behavior_cloning_policy(
        train_records, manifest["dataset_version"], epochs=epochs
    )
    output_dir = Path(models_root) / "dispatch_policy" / str(model_version)
    output_dir.mkdir(parents=True, exist_ok=False)
    model_path = output_dir / "model.json"
    model_path.write_text(
        json.dumps(policy.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics = {
        "train": evaluate_global_behavior_cloning_policy(policy, train_records),
        "validation": evaluate_global_behavior_cloning_policy(
            policy, validation_records
        ),
        "test": evaluate_global_behavior_cloning_policy(policy, test_records),
    }
    report = {
        "model_type": MODEL_TYPE,
        "model_version": str(model_version),
        "policy_version": policy.policy_version,
        "status": "TRAINED_OFFLINE_SHADOW_ONLY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": manifest["dataset_version"],
        "dataset_schema": global_view.get("schema_version"),
        "dataset_manifest_path": str(manifest_path),
        "model_path": str(model_path),
        "training": training,
        "metrics": metrics,
        "promotion_status": "NOT_PROMOTED",
        "execution_authority": "shadow_only",
        "deployment_scope": "s01_global_assignment_only",
        "other_decision_paths": {
            "s02_failure_reassignment": "candidate_bc_v1_or_rule_baseline",
            "s07_route_replanning": "RoadGraph_deterministic_planner",
        },
        "safety_requirements": [
            "hard_constraints_before_global_assignment",
            "unique_vehicle_assignment_enforced",
            "safety_shield_before_execution",
            "fallback_to_heuristic_baseline_on_invalid_output",
            "carla_ab_validation_required_before_promotion",
        ],
    }
    report_path = output_dir / "training_report.json"
    report["training_report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
