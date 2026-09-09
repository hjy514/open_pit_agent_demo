"""Map-resource-backed, CARLA-free random S01 structural runner."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from ..decision import (CandidateCostInput, MapContext, MultiObjectiveCostModel,
                        OptimizationAssignmentAdapter, OptimizationScheduler,
                        load_cost_weights,
                        review_selected_candidate_rankings)
from ..models import Task, VehicleState
from ..map_resources.route_coverage import BOUNDARY
from ..scheduler import BaselineScheduler
from .mock_runner import run_s01_structural_mock
from .generator import ROLE_DETAILS, generate_map_constrained_workload


def prepare_random_map_workload(config: Any, seed: Optional[int] = None,
                                vehicle_count: int = 6,
                                minimum_length_m: float = 500.0,
                                maximum_length_m: float = 3000.0,
                                scenario_key: str = "s01",
                                eligible_pairs: Optional[Set[Tuple[str, str]]] = None,
                                deadhead_eligible_pairs: Optional[
                                    Set[Tuple[str, str]]
                                ] = None,
                                ) -> Dict[str, Any]:
    """Compatibility name for the unified map-constrained generator."""
    return generate_map_constrained_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m,
        scenario_key, eligible_pairs, deadhead_eligible_pairs,
    )


def run_random_s01_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   execution_policy: str = "heuristic",
                                   eligible_pairs: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Run a seeded global-map S01 draft through the existing Mock scheduler."""
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m, "s01",
        eligible_pairs=eligible_pairs,
    )
    dynamic = workload["config"]
    effective_seed = workload["seed"]
    vehicles = workload["vehicles"]
    task_templates = deepcopy(workload["tasks"])
    tasks = workload["task_drafts"]
    vehicle_origins = workload["vehicle_origins"]
    zone_targets = workload["zone_targets"]
    route_costs = workload["route_costs"]
    route_validation_statuses = workload["route_validation_statuses"]

    def route_distance(vehicle, zone):
        value = route_costs.get((vehicle_origins[vehicle.vehicle_id], zone_targets[zone.zone_id]))
        if value is None or not minimum_length_m <= float(value) <= maximum_length_m:
            return None
        return value

    scheduler = BaselineScheduler(
        load_penalty=10000.0,
        distance_provider=route_distance,
        distance_label="p5_route_length",
        constrained_tasks_first=True,
        unique_vehicle_assignment=True,
    )
    baseline_result = run_s01_structural_mock(
        dynamic, effective_seed, tasks=deepcopy(task_templates), scheduler=scheduler,
        include_candidate_rankings=True,
    )
    cost_config = Path(__file__).resolve().parents[3] / "configs" / "dispatch_cost_v1.json"
    cost_model = MultiObjectiveCostModel(load_cost_weights(cost_config))
    task_objects = {item.task_id: item for item in task_templates}
    v0_selected = {
        item["task_id"]: item["vehicle_id"]
        for item in baseline_result["assignments"]
    }
    candidates_by_task = {}
    for task_id in sorted(task_objects):
        task = task_objects[task_id]
        shadow_inputs = []
        for vehicle_config in vehicles:
            route_length = route_costs.get((vehicle_origins[vehicle_config.vehicle_id], zone_targets[task.zone_id]))
            vehicle_state = VehicleState(
                vehicle_id=vehicle_config.vehicle_id,
                display_name=vehicle_config.display_name,
                equipment_type=vehicle_config.equipment_type,
                role_name=vehicle_config.role_name,
                blueprint=vehicle_config.blueprint,
                capabilities=list(vehicle_config.capabilities),
                position=vehicle_config.mock_position,
            )
            shadow_inputs.append(CandidateCostInput(
                vehicle=vehicle_state,
                task=task,
                map_context=MapContext(
                    route_reachable=route_length is not None,
                    route_length_m=route_length,
                    planner_version="CARLA_GlobalRoutePlanner_0.9.10_res_2.000m",
                    validation_status=route_validation_statuses.get(
                        (vehicle_origins[vehicle_config.vehicle_id], zone_targets[task.zone_id])
                    ) if route_length is not None else None,
                ),
                runtime_state={
                    "minimum_route_length_m": minimum_length_m,
                    "maximum_route_length_m": maximum_length_m,
                },
                metrics={
                    "nominal_speed_mps": vehicle_config.target_speed_kmh / 3.6,
                    "wait_time_s": 0.0,
                    "task_delay_s": 0.0,
                    "active_task_count": 0.0,
                },
                metric_status={
                    "wait_time_s": "surrogate_only",
                    "task_delay_s": "surrogate_only",
                    "active_task_count": "available",
                },
            ))
        candidates_by_task[task_id] = shadow_inputs
    optimization = OptimizationScheduler(cost_model).optimize(candidates_by_task)
    baseline_safety_reviews = review_selected_candidate_rankings(
        optimization.candidate_rankings, v0_selected
    )
    if execution_policy == "heuristic":
        result = run_s01_structural_mock(
            dynamic, effective_seed, tasks=deepcopy(task_templates),
            scheduler=scheduler, include_candidate_rankings=True,
            execution_context={
                "issued_by": "heuristic-route-load-global-unique-v0",
                "safety_gate_status": (
                    "APPROVED" if all(
                        item.get("status") in {"APPROVED", "MODIFIED"}
                        for item in baseline_safety_reviews
                    ) else "REJECTED"
                ),
                "safety_reviews": baseline_safety_reviews,
            },
            closed_loop_coordination=True,
        )
    elif execution_policy == "multi-objective":
        result = run_s01_structural_mock(
            dynamic, effective_seed, tasks=deepcopy(task_templates),
            scheduler=OptimizationAssignmentAdapter(optimization),
            include_candidate_rankings=True,
            execution_context={
                "issued_by": "multi_objective_optimizer",
                "safety_gate_status": (
                    "APPROVED" if all(
                        item.get("status") in {"APPROVED", "MODIFIED"}
                        for item in optimization.safety_reviews
                    ) else "REJECTED"
                ),
                "safety_reviews": optimization.safety_reviews,
            },
            closed_loop_coordination=True,
        )
    else:
        raise ValueError("unsupported S01 execution policy: {}".format(
            execution_policy
        ))
    v1_selected_by_task = {item["task_id"]: item["vehicle_id"]
                           for item in optimization.assignments}

    ranking_by_pair = {
        (task_id, str(candidate["vehicle_id"])): candidate
        for task_id, ranking in optimization.candidate_rankings.items()
        for candidate in ranking if candidate.get("feasible")
    }

    def fleet_metric(mapping, field, nested=None):
        values = []
        for task_id, vehicle_id in mapping.items():
            candidate = ranking_by_pair[(task_id, vehicle_id)]
            value = candidate[field] if nested is None else candidate[field][nested]
            if value is None:
                return None
            values.append(float(value))
        return round(sum(values), 6)

    v0_cost = fleet_metric(v0_selected, "total_cost")
    v1_cost = fleet_metric(v1_selected_by_task, "total_cost")
    v0_route = fleet_metric(v0_selected, "cost_transport", "value")
    v1_route = fleet_metric(v1_selected_by_task, "cost_transport", "value")
    comparisons = []
    for task_id in sorted(task_objects):
        v1_selected = v1_selected_by_task.get(task_id)
        comparisons.append({
            "task_id": task_id,
            "executed_vehicle_v0": v0_selected.get(task_id),
            "shadow_selected_vehicle_v1": v1_selected,
            "selection_changed": v0_selected.get(task_id) != v1_selected,
            "v1_candidate_ranking": optimization.candidate_rankings[task_id],
        })
    result.update({
        "scenario_source": "map_resources_global_p5",
        "map_resource_task_draft": tasks,
        "boundary": BOUNDARY,
        "random_mode": "seeded_structural_mock_only",
        "policy_comparison": {
            "mode": (
                "multi_objective_v1_executed_with_v0_baseline"
                if execution_policy == "multi-objective"
                else "shadow_only_v1_not_executed"
            ),
            "requested_policy": execution_policy,
            "executed_policy": (
                "multi-objective-cost-v1"
                if execution_policy == "multi-objective"
                else "heuristic-route-load-global-unique-v0"
            ),
            "comparison_policy": (
                "heuristic-route-load-global-unique-v0"
                if execution_policy == "multi-objective"
                else "multi-objective-cost-v1"
            ),
            "shadow_policy": "multi-objective-cost-v1",
            "optimizer_version": optimization.optimizer_version,
            "optimizer_status": optimization.status,
            "safety_shield_reviews": (
                optimization.safety_reviews
                if execution_policy == "multi-objective"
                else baseline_safety_reviews
            ),
            "shadow_safety_shield_reviews": (
                optimization.safety_reviews
                if execution_policy == "heuristic" else []
            ),
            "safety_shield": {
                "mode": "execution_gate",
                "reviews": (
                    optimization.safety_reviews
                    if execution_policy == "multi-objective"
                    else baseline_safety_reviews
                ),
            },
            "fleet_total_cost": optimization.fleet_total_cost,
            "selection_changed_count": sum(
                v0_selected.get(task_id) != v1_selected_by_task.get(task_id)
                for task_id in task_objects
            ),
            "ab_metrics": {
                "heuristic_normalized_cost": v0_cost,
                "multi_objective_normalized_cost": v1_cost,
                "normalized_cost_improvement": (
                    round(v0_cost - v1_cost, 6)
                    if v0_cost is not None and v1_cost is not None else None
                ),
                "heuristic_route_length_m": v0_route,
                "multi_objective_route_length_m": v1_route,
                "route_length_change_m": (
                    round(v1_route - v0_route, 6)
                    if v0_route is not None and v1_route is not None else None
                ),
            },
            "cost_config": str(cost_config),
            "comparisons": comparisons,
        },
    })
    return result
