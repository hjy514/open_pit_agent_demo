"""Exact small-fleet optimizer using normalized multi-objective costs."""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .cost_model import (
    CandidateCostInput, CandidateCostResult, MultiObjectiveCostModel,
    SafetyShield,
)
from ..models import Assignment, utc_now


OPTIMIZER_VERSION = "global-unique-multi-objective-v1"


@dataclass
class OptimizationResult:
    status: str
    optimizer_version: str
    cost_policy_version: str
    fleet_total_cost: float
    assignments: List[Dict[str, Any]]
    candidate_rankings: Dict[str, List[Dict[str, Any]]]
    safety_reviews: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OptimizationScheduler:
    """Minimize fleet cost subject to one task per vehicle for 6--8 vehicle Mock runs."""

    def __init__(self, cost_model: MultiObjectiveCostModel,
                 safety_shield: Optional[SafetyShield] = None) -> None:
        self.cost_model = cost_model
        self.safety_shield = safety_shield or SafetyShield()

    def optimize(self, candidates_by_task: Dict[str, Sequence[CandidateCostInput]]) -> OptimizationResult:
        ranked: Dict[str, List[CandidateCostResult]] = {}
        inputs_by_pair = {}
        for task_id, candidates in candidates_by_task.items():
            for item in candidates:
                inputs_by_pair[(str(task_id), str(item.vehicle.vehicle_id))] = item
            results = self.cost_model.rank(candidates)
            for result in results:
                result.selected = False
            ranked[str(task_id)] = results
        ordered_tasks = sorted(ranked, key=lambda key: (
            sum(item.feasible for item in ranked[key]), key
        ))
        best_cost, best = None, None

        def search(index, used, total, chosen):
            nonlocal best_cost, best
            if best_cost is not None and total >= best_cost:
                return
            if index == len(ordered_tasks):
                best_cost, best = total, list(chosen)
                return
            task_id = ordered_tasks[index]
            for candidate in ranked[task_id]:
                if not candidate.feasible or candidate.total_cost is None:
                    continue
                if candidate.vehicle_id in used:
                    continue
                search(index + 1, used | {candidate.vehicle_id},
                       total + candidate.total_cost, chosen + [(task_id, candidate)])

        search(0, set(), 0.0, [])
        if best is None:
            return OptimizationResult(
                "INFEASIBLE", OPTIMIZER_VERSION, "multi-objective-cost-v1", 0.0, [],
                {key: [item.to_dict() for item in value] for key, value in ranked.items()},
            )
        assignments = []
        safety_reviews = []
        for task_id, candidate in best:
            candidate.selected = True
            review = self.safety_shield.review(
                inputs_by_pair[(str(task_id), candidate.vehicle_id)]
            ).to_dict()
            safety_reviews.append(review)
            assignments.append({"task_id": task_id, "vehicle_id": candidate.vehicle_id,
                                "total_cost": candidate.total_cost,
                                "policy_version": candidate.policy_version,
                                "safety_review": review})
        if any(item["status"] == "REJECTED" for item in safety_reviews):
            return OptimizationResult(
                "SAFETY_REJECTED", OPTIMIZER_VERSION, "multi-objective-cost-v1",
                round(float(best_cost), 6), assignments,
                {key: [item.to_dict() for item in value]
                 for key, value in ranked.items()}, safety_reviews,
            )
        return OptimizationResult(
            "OPTIMAL", OPTIMIZER_VERSION, "multi-objective-cost-v1",
            round(float(best_cost), 6), assignments,
            {key: [item.to_dict() for item in value] for key, value in ranked.items()},
            safety_reviews,
        )


class OptimizationAssignmentAdapter:
    """Execute one solved result through the structural runner contract."""

    def __init__(self, result: OptimizationResult) -> None:
        if result.status != "OPTIMAL":
            raise ValueError("cannot execute non-optimal assignment: {}".format(
                result.status
            ))
        self.result = result
        self._selected = {
            str(item["task_id"]): item for item in result.assignments
        }

    def rank_candidates(self, task: Any, vehicles: Sequence[Any],
                        zones: Sequence[Any], active_tasks: Sequence[Any] = (),
                        excluded_vehicle_ids: Iterable[str] = ()) -> List[Assignment]:
        excluded = set(excluded_vehicle_ids)
        output = []
        for item in self.result.candidate_rankings.get(str(task.task_id), []):
            if not item.get("feasible") or item.get("vehicle_id") in excluded:
                continue
            output.append(Assignment(
                task_id=str(task.task_id), zone_id=str(task.zone_id),
                vehicle_id=str(item["vehicle_id"]),
                score=float(item["total_cost"]),
                reason=(
                    "hard constraints passed; normalized multi-objective "
                    "cost={:.6f}; policy={}"
                ).format(float(item["total_cost"]), item.get("policy_version")),
            ))
        return output

    def assign(self, tasks: Sequence[Any], vehicles: Sequence[Any],
               zones: Sequence[Any],
               excluded_vehicle_ids: Iterable[str] = ()) -> List[Assignment]:
        excluded = set(excluded_vehicle_ids)
        vehicle_by_id = {
            str(vehicle.vehicle_id): vehicle for vehicle in vehicles
        }
        assignments = []
        for task in tasks:
            if task.status in {"completed", "timed_out", "cancelled"}:
                continue
            if task.assigned_vehicle_id:
                current = vehicle_by_id.get(str(task.assigned_vehicle_id))
                if (current is not None and current.available
                        and current.health == "healthy"
                        and current.vehicle_id not in excluded):
                    continue
            selected = self._selected.get(str(task.task_id))
            if selected is None or selected["vehicle_id"] in excluded:
                raise ValueError(
                    "optimized assignment unavailable for task {}".format(task.task_id)
                )
            review = selected.get("safety_review", {})
            if review.get("status") not in {"APPROVED", "MODIFIED"}:
                raise ValueError(
                    "optimized assignment has no safety approval for task {}".format(
                        task.task_id
                    )
                )
            task.assigned_vehicle_id = str(selected["vehicle_id"])
            task.status = "assigned"
            task.updated_at = utc_now()
            task.status_reason = "assigned_by_multi_objective_optimizer"
            assignments.append(Assignment(
                task_id=str(task.task_id), zone_id=str(task.zone_id),
                vehicle_id=str(selected["vehicle_id"]),
                score=float(selected["total_cost"]),
                reason=(
                    "global unique assignment after hard constraints; "
                    "normalized multi-objective cost={:.6f}; optimizer={}"
                ).format(float(selected["total_cost"]), self.result.optimizer_version),
            ))
        return assignments
