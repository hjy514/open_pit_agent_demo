"""Hard-constraint-first, normalized multi-objective dispatch cost model V1.

This module does not replace ``BaselineScheduler``.  It is an offline,
simulator-independent cost contract for the next optimization baseline.
Static route facts come from map_resources; traffic, risk, weather and road
closures are supplied per candidate as runtime state.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


POLICY_VERSION = "multi-objective-cost-v1"
SAFETY_SHIELD_VERSION = "deterministic-safety-shield-v1"
AVAILABLE = "available"
NOT_AVAILABLE = "not_available"
SURROGATE_ONLY = "surrogate_only"
PENDING_CARLA_VALIDATION = "pending_carla_validation"


@dataclass(frozen=True)
class MapContext:
    route_reachable: Optional[bool]
    route_length_m: Optional[float] = None
    route_edge_ids: Tuple[str, ...] = ()
    planner_version: Optional[str] = None
    validation_status: Optional[str] = None


@dataclass(frozen=True)
class CandidateCostInput:
    vehicle: Any
    task: Any
    map_context: MapContext
    runtime_state: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    metric_status: Dict[str, str] = field(default_factory=dict)


@dataclass
class CandidateCostResult:
    vehicle_id: str
    task_id: str
    feasible: bool
    constraint_results: List[Dict[str, Any]]
    costs: Dict[str, Dict[str, Any]]
    total_cost: Optional[float]
    policy_version: str = POLICY_VERSION
    selected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.update(self.costs)
        payload.pop("costs")
        return payload


def load_cost_weights(path: Path) -> Dict[str, float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    weights = payload.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("cost config requires non-empty weights")
    result = {str(name): float(value) for name, value in weights.items()}
    if any(value < 0 for value in result.values()) or not any(result.values()):
        raise ValueError("cost weights must be non-negative and not all zero")
    return result


class HardConstraintEvaluator:
    """Return explicit constraint results; any failed item removes a candidate."""

    def evaluate(self, item: CandidateCostInput) -> List[Dict[str, Any]]:
        vehicle, task = item.vehicle, item.task
        runtime, map_context = item.runtime_state, item.map_context
        closed_edges = set(runtime.get("closed_edge_ids", ()))
        route_edges = set(map_context.route_edge_ids)
        required = set(getattr(task, "required_capabilities", ()))
        capabilities = set(getattr(vehicle, "capabilities", ()))
        required_capacity = runtime.get("required_capacity")
        vehicle_capacity = runtime.get("vehicle_capacity")
        route_length = map_context.route_length_m
        minimum_route = runtime.get("minimum_route_length_m")
        maximum_route = runtime.get("maximum_route_length_m")
        route_in_range = route_length is not None and (
            minimum_route is None or float(route_length) >= float(minimum_route)
        ) and (
            maximum_route is None or float(route_length) <= float(maximum_route)
        )
        checks = [
            ("vehicle_available", bool(getattr(vehicle, "available", False)),
             "vehicle unavailable"),
            ("vehicle_healthy", getattr(vehicle, "health", None) == "healthy",
             "vehicle health is not healthy"),
            ("capability_match", required.issubset(capabilities),
             "required capabilities are not satisfied"),
            ("route_reachable", map_context.route_reachable is True,
             "route is unreachable or reachability is unknown"),
            ("route_operational_limit", route_in_range,
             "route length is missing or outside the scenario operational range"),
            ("road_open", not bool(closed_edges.intersection(route_edges)),
             "route contains a closed edge"),
            ("red_zone_access",
             not bool(runtime.get("target_in_red_zone"))
             or bool(runtime.get("red_zone_authorized")),
             "RED zone entry is not authorized"),
            ("task_capacity",
             required_capacity is None or (
                 vehicle_capacity is not None
                 and float(vehicle_capacity) >= float(required_capacity)
             ), "vehicle capacity does not satisfy task capacity"),
        ]
        return [{"constraint": name, "passed": passed,
                 "reason": "passed" if passed else reason}
                for name, passed, reason in checks]


@dataclass
class SafetyReview:
    """Independent pre-execution decision review."""

    status: str
    vehicle_id: str
    task_id: str
    constraint_results: List[Dict[str, Any]]
    fallback_required: bool
    reason: str
    modifications: Dict[str, Any] = field(default_factory=dict)
    shield_version: str = SAFETY_SHIELD_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SafetyShield:
    """Recheck a selected candidate immediately before task execution.

    The shield never optimizes or silently selects another vehicle.  A rejected
    decision must return to a deterministic scheduler fallback.  Optional safe
    modifications are explicit in runtime state and therefore auditable.
    """

    def __init__(self, constraints: Optional[HardConstraintEvaluator] = None) -> None:
        self.constraints = constraints or HardConstraintEvaluator()

    def review(self, item: CandidateCostInput) -> SafetyReview:
        return self.review_explicit(
            vehicle_id=str(item.vehicle.vehicle_id),
            task_id=str(item.task.task_id),
            constraint_results=self.constraints.evaluate(item),
            requires_human_confirmation=bool(
                item.runtime_state.get("requires_human_confirmation")
            ),
            human_confirmed=bool(item.runtime_state.get("human_confirmed")),
            modifications=item.runtime_state.get("safety_modifications", {}),
        )

    def review_explicit(
        self, vehicle_id: str, task_id: str,
        constraint_results: Any,
        requires_human_confirmation: bool = False,
        human_confirmed: bool = False,
        modifications: Optional[Dict[str, Any]] = None,
    ) -> SafetyReview:
        """Review already-computed scenario facts through one output contract."""
        if isinstance(constraint_results, dict):
            checks = [{
                "constraint": str(name),
                "passed": bool(passed),
                "reason": "passed" if passed else "explicit constraint failed",
            } for name, passed in sorted(constraint_results.items())]
        elif isinstance(constraint_results, list):
            checks = [dict(item) for item in constraint_results
                      if isinstance(item, dict)]
        else:
            checks = []
        if not checks:
            checks.append({
                "constraint": "explicit_safety_facts_present",
                "passed": False,
                "reason": "no explicit safety constraint evidence",
            })
        checks.append({
            "constraint": "human_confirmation",
            "passed": not requires_human_confirmation or human_confirmed,
            "reason": (
                "passed" if not requires_human_confirmation or human_confirmed
                else "high-risk decision requires human confirmation"
            ),
        })
        failed = [check for check in checks if not check["passed"]]
        if not isinstance(modifications, dict):
            modifications = {}
        if failed:
            status = "REJECTED"
            reason = "; ".join(check["reason"] for check in failed)
        elif modifications:
            status = "MODIFIED"
            reason = "approved with explicit safety modifications"
        else:
            status = "APPROVED"
            reason = "all deterministic safety constraints passed"
        return SafetyReview(
            status=status,
            vehicle_id=str(vehicle_id),
            task_id=str(task_id),
            constraint_results=checks,
            fallback_required=status == "REJECTED",
            reason=reason,
            modifications=dict(modifications),
        )


def review_selected_candidate_rankings(
    candidate_rankings: Dict[str, Sequence[Dict[str, Any]]],
    selected_by_task: Dict[str, str],
    safety_shield: Optional[SafetyShield] = None,
) -> List[Dict[str, Any]]:
    """Apply the Safety Shield to an already selected baseline assignment.

    Candidate rankings remain the source of the hard-constraint facts.  This
    helper does not rank candidates or change a selected vehicle; it only
    gives non-optimization policies the same auditable pre-execution safety
    contract as the optimization scheduler.
    """
    shield = safety_shield or SafetyShield()
    reviews = []
    for task_id in sorted(selected_by_task):
        vehicle_id = str(selected_by_task[task_id])
        selected = next((
            item for item in candidate_rankings.get(str(task_id), ())
            if str(item.get("vehicle_id")) == vehicle_id
        ), None)
        constraint_results = (
            selected.get("constraint_results") if selected is not None else []
        )
        reviews.append(shield.review_explicit(
            vehicle_id=vehicle_id,
            task_id=str(task_id),
            constraint_results=constraint_results,
        ).to_dict())
    return reviews


class MultiObjectiveCostModel:
    """Rank feasible candidates by normalized, weighted objective components."""

    COMPONENTS = (
        "cost_time", "cost_wait", "cost_delay", "cost_transport",
        "cost_workload", "cost_recovery", "cost_switch", "cost_prod",
        "cost_idle", "cost_energy",
    )

    def __init__(self, weights: Dict[str, float],
                 constraints: Optional[HardConstraintEvaluator] = None,
                 include_surrogate_costs: bool = False) -> None:
        self.weights = {name: float(weights.get(name, 0.0)) for name in self.COMPONENTS}
        if any(value < 0 for value in self.weights.values()) or not any(self.weights.values()):
            raise ValueError("cost weights must be non-negative and not all zero")
        self.constraints = constraints or HardConstraintEvaluator()
        self.include_surrogate_costs = bool(include_surrogate_costs)

    @staticmethod
    def _metric(item: CandidateCostInput, name: str, unit: str,
                source: str) -> Dict[str, Any]:
        value = item.metrics.get(name)
        status = item.metric_status.get(name, AVAILABLE if value is not None else NOT_AVAILABLE)
        return {"value": None if value is None else float(value), "normalized": None,
                "unit": unit, "availability": status, "source": source}

    def _raw_costs(self, item: CandidateCostInput) -> Dict[str, Dict[str, Any]]:
        route_length = item.map_context.route_length_m
        time_cost = self._metric(item, "eta_seconds", "s", "runtime_or_estimator")
        if time_cost["value"] is None and route_length is not None:
            speed = item.metrics.get("nominal_speed_mps")
            if speed is not None and float(speed) > 0:
                time_cost = {"value": float(route_length) / float(speed), "normalized": None,
                             "unit": "s", "availability": SURROGATE_ONLY,
                             "source": "route_length_divided_by_nominal_speed"}
            else:
                time_cost["availability"] = PENDING_CARLA_VALIDATION
        switching = float(bool(getattr(item.vehicle, "current_task_id", None)
                               and getattr(item.vehicle, "current_task_id", None)
                               != getattr(item.task, "task_id", None)))
        return {
            "cost_time": time_cost,
            "cost_wait": self._metric(item, "wait_time_s", "s", "runtime_state"),
            "cost_delay": self._metric(item, "task_delay_s", "s", "runtime_state"),
            "cost_transport": {"value": None if route_length is None else float(route_length),
                               "normalized": None, "unit": "m",
                               "availability": AVAILABLE if route_length is not None else NOT_AVAILABLE,
                               "source": "map_resources.route_length_m"},
            "cost_workload": self._metric(item, "active_task_count", "task", "runtime_state"),
            "cost_recovery": self._metric(item, "recovery_time_s", "s", "runtime_state"),
            "cost_switch": {"value": switching, "normalized": None, "unit": "binary",
                            "availability": AVAILABLE, "source": "vehicle.current_task_id"},
            "cost_prod": self._metric(item, "production_deviation", "unknown", "not_available"),
            "cost_idle": self._metric(item, "resource_idle_time_s", "s", "not_available"),
            "cost_energy": self._metric(item, "energy_cost", "unknown", "not_available"),
        }

    def rank(self, candidates: Sequence[CandidateCostInput]) -> List[CandidateCostResult]:
        results = []
        for item in candidates:
            constraints = self.constraints.evaluate(item)
            results.append(CandidateCostResult(
                vehicle_id=str(item.vehicle.vehicle_id), task_id=str(item.task.task_id),
                feasible=all(check["passed"] for check in constraints),
                constraint_results=constraints, costs=self._raw_costs(item), total_cost=None,
            ))
        feasible = [result for result in results if result.feasible]
        for component in self.COMPONENTS:
            accepted_statuses = {AVAILABLE}
            if self.include_surrogate_costs:
                accepted_statuses.add(SURROGATE_ONLY)
            values = [result.costs[component]["value"] for result in feasible
                      if result.costs[component]["value"] is not None
                      and result.costs[component]["availability"] in accepted_statuses]
            if not values:
                continue
            low, high = min(values), max(values)
            for result in feasible:
                entry = result.costs[component]
                if entry["value"] is not None and entry["availability"] in accepted_statuses:
                    entry["normalized"] = 0.0 if high == low else (entry["value"] - low) / (high - low)
        for result in feasible:
            active = [(name, self.weights[name], result.costs[name]["normalized"])
                      for name in self.COMPONENTS
                      if self.weights[name] > 0 and result.costs[name]["normalized"] is not None]
            weight_sum = sum(weight for _, weight, _ in active)
            result.total_cost = round(sum(weight * value for _, weight, value in active) / weight_sum, 6)
        feasible.sort(key=lambda result: (result.total_cost, result.vehicle_id))
        if feasible:
            feasible[0].selected = True
        rejected = sorted((result for result in results if not result.feasible),
                          key=lambda result: result.vehicle_id)
        return feasible + rejected
