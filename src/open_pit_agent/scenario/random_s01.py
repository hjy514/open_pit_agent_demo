"""Map-resource-backed, CARLA-free random S01 structural runner."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from ..config import VehicleConfig, ZoneConfig
from ..decision import (CandidateCostInput, MapContext, MultiObjectiveCostModel,
                        OptimizationAssignmentAdapter, OptimizationScheduler,
                        load_cost_weights,
                        review_selected_candidate_rankings)
from ..models import Position, Task, VehicleState
from ..map_resources import MapResourceStore
from ..map_resources.route_coverage import BOUNDARY, constrained_seeded_tasks
from ..scheduler import BaselineScheduler
from .mock_runner import run_s01_structural_mock


ROLE_DETAILS = {
    "haul": ("运输车", "haul_truck", ["haul", "inspection"], ["haul"]),
    "inspection": ("巡检车", "inspection_vehicle", ["inspection", "slope_monitoring"], ["inspection", "slope_monitoring"]),
    "support": ("保障车", "support_vehicle", ["inspection", "emergency_support"], ["emergency_support"]),
}


def prepare_random_map_workload(config: Any, seed: Optional[int] = None,
                                vehicle_count: int = 6,
                                minimum_length_m: float = 500.0,
                                maximum_length_m: float = 3000.0,
                                scenario_key: str = "s01",
                                eligible_pairs: Optional[Set[Tuple[str, str]]] = None
                                ) -> Dict[str, Any]:
    """Resolve one reproducible multi-vehicle workload from static P5 facts."""
    binding = config.map_resource
    if binding is None or not binding.database_path or not binding.map_id or not binding.resource_version:
        raise ValueError("random map workload requires a map_resource database binding")
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    with MapResourceStore(binding.database_path) as store:
        points = {item["point_id"]: item for item in store.verified_spawn_points(binding.map_id)}
        planner_routes = list(store.planner_reachable_pairs(binding.map_id, binding.resource_version))
        if eligible_pairs is not None:
            planner_routes = [
                item for item in planner_routes
                if (item["from_point_id"], item["to_point_id"]) in eligible_pairs
            ]
        tasks = constrained_seeded_tasks(
            planner_routes,
            store.blocked_dual_spawn_pairs(binding.map_id, binding.resource_version),
            effective_seed, vehicle_count, minimum_length_m, maximum_length_m,
            scenario_key=scenario_key,
        )
    vehicles, zones, runtime_tasks = [], [], []
    for item in tasks:
        role = item["vehicle_role"]
        display_prefix, equipment_type, capabilities, requirements = ROLE_DETAILS[role]
        start, target = points[item["from_point_id"]], points[item["to_point_id"]]
        vehicles.append(VehicleConfig(
            vehicle_id=item["vehicle_id"], display_name="{}{:02d}".format(display_prefix, item["vehicle_slot"]),
            equipment_type=equipment_type, role_name=role, blueprint="vehicle.cat.cat",
            spawn_point_index=int(start["spawn_point_index"]),
            mock_position=Position(float(start["x"]), float(start["y"]), float(start["z"])),
            target_speed_kmh=25.0 if role == "haul" else 20.0,
            capabilities=list(capabilities),
        ))
        zones.append(ZoneConfig(
            zone_id=item["task_id"], display_name="随机{}任务区".format(role),
            priority=60 if role == "haul" else 50 if role == "inspection" else 45,
            required_capabilities=list(requirements),
            target_spawn_point_index=int(target["spawn_point_index"]),
            mock_position=Position(float(target["x"]), float(target["y"]), float(target["z"])),
            preferred_vehicle_id=None,
        ))
        runtime_tasks.append(Task(
            task_id=item["task_id"], zone_id=item["task_id"],
            priority=60 if role == "haul" else 50 if role == "inspection" else 45,
            required_capabilities=list(requirements), preferred_vehicle_id=None,
            task_type="haul_transport" if role == "haul" else "slope_inspection" if role == "inspection" else "equipment_support",
        ))
    dynamic = replace(config, scenario_id="{}-{}v-seed-{}".format(
                          str(scenario_key).lower(), vehicle_count, effective_seed),
                      vehicles=vehicles, zones=zones,
                      fleet=replace(config.fleet, total_vehicles=vehicle_count,
                                    available_vehicles=vehicle_count, active_vehicles=vehicle_count,
                                    traffic_vehicles=0, role_policy="fixed"))
    vehicle_origins = {item["vehicle_id"]: item["from_point_id"] for item in tasks}
    zone_targets = {item["task_id"]: item["to_point_id"] for item in tasks}
    route_costs = {
        (item["from_point_id"], item["to_point_id"]): item["route_length_m"]
        for item in planner_routes
    }

    return {
        "config": dynamic,
        "seed": effective_seed,
        "vehicle_count": vehicle_count,
        "vehicles": vehicles,
        "zones": zones,
        "tasks": runtime_tasks,
        "task_drafts": tasks,
        "vehicle_origins": vehicle_origins,
        "zone_targets": zone_targets,
        "route_costs": route_costs,
        "minimum_length_m": minimum_length_m,
        "maximum_length_m": maximum_length_m,
    }


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
                    validation_status="PLANNER_REACHABLE" if route_length is not None else None,
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
