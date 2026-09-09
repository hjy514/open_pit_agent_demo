"""Seeded loading-equipment failure and alternative work-point scenario."""
from copy import deepcopy
from random import Random
from typing import Any, Dict, Optional, Set, Tuple

from ..adapters.mock_adapter import MockAdapter
from ..map_resources import MapResourceStore
from ..map_resources.road_graph import route_plans_from_store
from ..scheduler import BaselineScheduler
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot
from .generator import sample_event_timing
from .random_s01 import prepare_random_map_workload


POLICY_VERSION = "equipment-failure-work-point-switch-v1"


def _event_parameters(config: Any, seed: int) -> Dict[str, Any]:
    randomization = config.scenario_variables.get("randomization", {})
    events = randomization.get("events", []) if isinstance(randomization, dict) else []
    raw = events[0].get("parameters", {}) if events else {}
    delay_range = raw.get("work_point_switch_delay_seconds_range", [30.0, 60.0])
    if (not isinstance(delay_range, list) or len(delay_range) != 2
            or float(delay_range[0]) < 0
            or float(delay_range[0]) > float(delay_range[1])):
        raise ValueError("S03 work_point_switch_delay_seconds_range is invalid")
    timing = sample_event_timing(config, "s03", seed)
    return {
        **timing,
        "work_point_switch_delay_s": round(Random(seed + 3003).uniform(
            float(delay_range[0]), float(delay_range[1])
        ), 6),
    }


def run_random_s03_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Switch one affected haul task to a reachable alternative work point.

    The equipment fault and switch delay are parameterized scenario inputs.
    Endpoints and route lengths are static map-resource facts. No equipment
    physics, CARLA traversal or real production timing is inferred.
    """
    binding = config.map_resource
    with MapResourceStore(binding.database_path) as store:
        eligible_rows = list(store.connection.execute(
            "SELECT from_point_id,to_point_id,route_length_m "
            "FROM route_candidates WHERE map_id=? AND resource_version=? "
            "AND validation_status='TOPOLOGY_DERIVED_UNVERIFIED'",
            (binding.map_id, binding.resource_version),
        ))
        eligible_pairs = {(str(row[0]), str(row[1])) for row in eligible_rows}
    if eligible_pairs_override is not None:
        eligible_pairs &= set(eligible_pairs_override)
    if not eligible_pairs:
        raise ValueError(
            "S03 has no topology-consistent route candidates; run "
            "./map_resources.sh build-route-candidates"
        )
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m,
        "s03", eligible_pairs=eligible_pairs,
    )
    dynamic = workload["config"]
    effective_seed = workload["seed"]
    parameters = _event_parameters(config, effective_seed)
    tasks = deepcopy(workload["tasks"])
    vehicle_origins = workload["vehicle_origins"]
    zone_targets = workload["zone_targets"]
    route_costs = workload["route_costs"]

    def route_distance(vehicle, zone):
        value = route_costs.get(
            (vehicle_origins[vehicle.vehicle_id], zone_targets[zone.zone_id])
        )
        if value is None or not minimum_length_m <= float(value) <= maximum_length_m:
            return None
        return value

    scheduler = BaselineScheduler(
        load_penalty=10000.0, distance_provider=route_distance,
        distance_label="p5_route_length", constrained_tasks_first=True,
        unique_vehicle_assignment=True,
    )
    episode = build_episode(dynamic, run_id="s03-random-structural-mock",
                            seed=effective_seed)
    adapter = MockAdapter(dynamic)
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        assignments = scheduler.assign(tasks, states, dynamic.zones)
        adapter.dispatch(tasks, dynamic.zones)
        initial_task_states = [task.to_dict() for task in tasks]
        task_by_id = {item.task_id: item for item in tasks}
        vehicle_by_id = {item.vehicle_id: item for item in dynamic.vehicles}
        assignment_by_task = {item.task_id: item for item in assignments}
        endpoints = {
            item.task_id: (
                vehicle_origins[item.vehicle_id], zone_targets[item.task_id]
            ) for item in assignments
        }
        haul_assignments = [
            item for item in assignments
            if task_by_id[item.task_id].task_type == "haul_transport"
        ]
        if not haul_assignments:
            raise ValueError("S03 requires at least one assigned haul task")
        affected = Random(effective_seed + 3030).choice(sorted(
            haul_assignments, key=lambda item: item.task_id
        ))
        affected_task_id = affected.task_id
        affected_origin, failed_point_id = endpoints[affected_task_id]
        active_targets = set(zone_targets.values())
        alternative_candidates = sorted(
            (
                {
                    "point_id": str(row[1]),
                    "route_length_m": float(row[2]),
                }
                for row in eligible_rows
                if str(row[0]) == affected_origin
                and str(row[1]) != failed_point_id
                and str(row[1]) not in active_targets
                and row[2] is not None
                and minimum_length_m <= float(row[2]) <= maximum_length_m
            ),
            key=lambda item: (item["route_length_m"], item["point_id"]),
        )
        if not alternative_candidates:
            raise ValueError(
                "S03 affected vehicle has no distinct reachable alternative work point"
            )
        # Seeded choice among the five shortest valid alternatives prevents a
        # fixed endpoint while keeping excessive detours out of the scenario.
        alternative = Random(effective_seed + 3031).choice(
            alternative_candidates[:5]
        )
        alternative_point_id = alternative["point_id"]
        alternative_endpoints = {
            affected_task_id: (affected_origin, alternative_point_id)
        }

        with MapResourceStore(binding.database_path) as store:
            original_plans = route_plans_from_store(
                store, binding.map_id, binding.resource_version, endpoints
            )
            alternative_plans = route_plans_from_store(
                store, binding.map_id, binding.resource_version,
                alternative_endpoints,
            )
        if len(original_plans) != len(assignments):
            raise ValueError("S03 requires a topology route for every active task")
        if affected_task_id not in alternative_plans:
            raise ValueError("S03 alternative work point has no topology route")

        route_plan_records = []
        for task_id in sorted(original_plans):
            assignment = assignment_by_task[task_id]
            start_id, goal_id = endpoints[task_id]
            route_plan_records.append({
                "route_plan_id": "{}:original-work-point".format(task_id),
                "vehicle_id": assignment.vehicle_id, "task_id": task_id,
                "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                "start_node": start_id, "goal_node": goal_id,
                "distance_m": float(route_costs[(start_id, goal_id)]),
                "status": (
                    "superseded_equipment_unavailable"
                    if task_id == affected_task_id else "unchanged"
                ),
                "edge_ids": original_plans[task_id],
            })
        original_distance = float(route_costs[(affected_origin, failed_point_id)])
        alternative_distance = float(alternative["route_length_m"])
        speed_mps = float(vehicle_by_id[affected.vehicle_id].target_speed_kmh) / 3.6
        travel_delta = (alternative_distance - original_distance) / speed_mps
        estimated_delay = float(parameters["work_point_switch_delay_s"]) + max(
            0.0, travel_delta
        )
        route_plan_records.append({
            "route_plan_id": "{}:alternative-work-point".format(affected_task_id),
            "vehicle_id": affected.vehicle_id, "task_id": affected_task_id,
            "planner_version": "RoadGraph-Dijkstra-Topology-V1",
            "start_node": affected_origin, "goal_node": alternative_point_id,
            "distance_m": alternative_distance, "status": "planned",
            "replan_reason": "loading_equipment_failure",
            "edge_ids": alternative_plans[affected_task_id],
        })
        decision = {
            "task_id": affected_task_id, "vehicle_id": affected.vehicle_id,
            "action_type": "switch_to_alternative_work_point",
            "failed_equipment_id": "loader@{}".format(failed_point_id),
            "alternative_equipment_id": "loader@{}".format(alternative_point_id),
            "failed_work_point_id": failed_point_id,
            "alternative_work_point_id": alternative_point_id,
            "original_distance_m": round(original_distance, 6),
            "alternative_distance_m": round(alternative_distance, 6),
            "estimated_travel_time_change_s": round(travel_delta, 6),
            "work_point_switch_delay_s": parameters["work_point_switch_delay_s"],
            "estimated_task_delay_s": round(estimated_delay, 6),
            "original_edge_ids": original_plans[affected_task_id],
            "selected_edge_ids": alternative_plans[affected_task_id],
            "route_source": "map_resources_topology_and_global_p5",
            "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
            "constraint_results": {
                "failed_equipment_excluded": True,
                "alternative_point_distinct": True,
                "alternative_route_reachable": True,
                "vehicle_capability_retained": True,
            },
            "policy_version": POLICY_VERSION,
        }

        for task in tasks:
            task.status = "completed"
            task.completed_tick = parameters["recovery_tick"]
            task.status_reason = (
                "structural_mock_completion_at_alternative_work_point"
                if task.task_id == affected_task_id
                else "structural_mock_completion_unaffected"
            )
        adapter.complete_tasks(tasks)
        return {
            "status": "PASS", "mode": "mock_structural",
            "simulation_claim": (
                "equipment_failure_and_work_point_switch_surrogate_only_no_carla_physics"
            ),
            "scenario_id": dynamic.scenario_id, "seed": effective_seed,
            "scenario_source": "map_resources_topology_and_global_p5",
            "random_mode": "seeded_structural_equipment_failure_mock_only",
            "equipment_event": {
                "event_type": "loading_equipment_failure",
                "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                "equipment_id": decision["failed_equipment_id"],
                "work_point_id": failed_point_id,
                **parameters,
            },
            "equipment_status_during_event": "FAILED",
            "equipment_status_after_recovery": "AVAILABLE",
            "affected_task_ids": [affected_task_id],
            "affected_task_count": 1,
            "unaffected_task_count": len(tasks) - 1,
            "equipment_decisions": [decision],
            "work_point_switch_count": 1,
            "route_plans": route_plan_records,
            "route_planner_version": "RoadGraph-Dijkstra-Topology-V1",
            "policy_version": POLICY_VERSION,
            "assignment_count": len(assignments),
            "assignments": [item.to_dict() for item in assignments],
            "task_count": len(tasks),
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "tasks": [task.to_dict() for task in tasks],
            "map_resource_task_draft": workload["task_drafts"],
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_vehicle_states": initial_vehicle_states,
            "initial_task_states": initial_task_states,
            "final_vehicle_states": vehicle_state_snapshot(adapter.list_states()),
            "boundary": (
                "P5/topology facts plus parameterized equipment failure and "
                "time surrogate; no CARLA equipment physics, production, "
                "multi-vehicle driving or real-mine validation."
            ),
        }
    finally:
        adapter.close()
