"""Seeded extreme-weather and road-capacity degradation structural scenario."""
from copy import deepcopy
from random import Random
from typing import Any, Dict, Optional, Set, Tuple

from ..adapters.mock_adapter import MockAdapter
from ..map_resources import MapResourceStore, RoadGraph, RoutePlanner
from ..map_resources.road_graph import (
    route_plans_from_store, verified_point_anchors_from_store,
)
from ..scheduler import BaselineScheduler
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot
from .random_s01 import prepare_random_map_workload


POLICY_VERSION = "weather-aware-route-policy-v1"


def _parameters(config: Any, seed: int) -> Dict[str, float]:
    randomization = config.scenario_variables.get("randomization", {})
    events = randomization.get("events", []) if isinstance(randomization, dict) else []
    raw = events[0].get("parameters", {}) if events else {}
    speed_range = raw.get("restricted_speed_factor_range", [0.45, 0.65])
    rain_range = raw.get("rainfall_intensity_mm_h_range", [35.0, 70.0])
    if (not isinstance(speed_range, list) or len(speed_range) != 2
            or not 0 < float(speed_range[0]) <= float(speed_range[1]) <= 1):
        raise ValueError("S05 restricted_speed_factor_range must be within (0, 1]")
    if (not isinstance(rain_range, list) or len(rain_range) != 2
            or float(rain_range[0]) < 0 or float(rain_range[0]) > float(rain_range[1])):
        raise ValueError("S05 rainfall_intensity_mm_h_range is invalid")
    random = Random(int(seed) + 5005)
    return {
        "event_tick": int(raw.get("event_tick", 30)),
        "recovery_tick": int(raw.get("recovery_tick", 60)),
        "restricted_speed_factor": round(random.uniform(
            float(speed_range[0]), float(speed_range[1])
        ), 6),
        "rainfall_intensity_mm_h": round(random.uniform(
            float(rain_range[0]), float(rain_range[1])
        ), 3),
        "visibility_m": float(raw.get("visibility_m", 120.0)),
        "surface_condition": str(raw.get("surface_condition", "wet_slippery")),
    }


def run_random_s05_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Apply one reproducible weather restriction to part of an active fleet.

    ETA values are explicit route-length/target-speed engineering estimates;
    they are not CARLA measurements or real mine observations.
    """
    binding = config.map_resource
    with MapResourceStore(binding.database_path) as store:
        eligible_pairs = {
            (str(row[0]), str(row[1]))
            for row in store.connection.execute(
                "SELECT from_point_id,to_point_id FROM route_candidates "
                "WHERE map_id=? AND resource_version=? "
                "AND validation_status='TOPOLOGY_DERIVED_UNVERIFIED'",
                (binding.map_id, binding.resource_version),
            )
        }
    if eligible_pairs_override is not None:
        eligible_pairs &= set(eligible_pairs_override)
    if not eligible_pairs:
        raise ValueError(
            "S05 has no topology-consistent route candidates; run "
            "./map_resources.sh build-route-candidates"
        )
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m,
        "s05", eligible_pairs=eligible_pairs,
    )
    dynamic = workload["config"]
    effective_seed = workload["seed"]
    parameters = _parameters(config, effective_seed)
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
    episode = build_episode(dynamic, run_id="s05-random-structural-mock",
                            seed=effective_seed)
    adapter = MockAdapter(dynamic)
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        assignments = scheduler.assign(tasks, states, dynamic.zones)
        adapter.dispatch(tasks, dynamic.zones)
        initial_task_states = [task.to_dict() for task in tasks]
        endpoints = {
            item.task_id: (
                vehicle_origins[item.vehicle_id], zone_targets[item.task_id]
            ) for item in assignments
        }
        assignment_by_task = {item.task_id: item for item in assignments}
        vehicle_by_id = {item.vehicle_id: item for item in dynamic.vehicles}

        with MapResourceStore(binding.database_path) as store:
            plans = route_plans_from_store(
                store, binding.map_id, binding.resource_version, endpoints
            )
            if len(plans) != len(assignments):
                raise ValueError("S05 requires a topology route for every active task")
            graph = RoadGraph.from_store(store, binding.map_id,
                                         binding.resource_version)
            route_planner = RoutePlanner(graph)
            anchors = verified_point_anchors_from_store(
                store, graph, binding.map_id
            )
            internal_edges = sorted({
                edge_id for route in plans.values() for edge_id in route[1:-1]
            })
            candidates = []
            for edge_id in internal_edges:
                impacted = sorted(
                    task_id for task_id, route in plans.items()
                    if edge_id in route
                )
                if impacted and len(impacted) < len(plans):
                    candidates.append((edge_id, impacted))
            if not candidates:
                raise ValueError(
                    "S05 workload has no road edge that selectively affects the fleet"
                )
            degraded_edge_id, affected_task_ids = Random(
                effective_seed + 5050
            ).choice(candidates)

            decisions = []
            route_plans = []
            speed_factor = parameters["restricted_speed_factor"]
            for task_id in sorted(plans):
                assignment = assignment_by_task[task_id]
                start_id, goal_id = endpoints[task_id]
                start, goal = anchors[start_id], anchors[goal_id]
                original = route_planner.plan(start, goal).to_dict()
                speed_mps = vehicle_by_id[assignment.vehicle_id].target_speed_kmh / 3.6
                baseline_eta = float(original["distance_m"]) / speed_mps
                route_plans.append({
                    "route_plan_id": "{}:normal".format(task_id),
                    "vehicle_id": assignment.vehicle_id, "task_id": task_id,
                    "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                    "start_node": start_id, "goal_node": goal_id,
                    "distance_m": original["distance_m"],
                    "status": "weather_affected" if task_id in affected_task_ids else "unchanged",
                    "edge_ids": original["edge_ids"],
                })
                if task_id not in affected_task_ids:
                    continue
                degraded_distance = min(
                    float(original["distance_m"]),
                    float(graph.edges[degraded_edge_id].length_m),
                )
                restricted_eta = baseline_eta + degraded_distance / speed_mps * (
                    1.0 / speed_factor - 1.0
                )
                alternative = route_planner.plan(
                    start, goal, closed_edge_ids={degraded_edge_id}
                ).to_dict()
                alternative_eta = (
                    float(alternative["distance_m"]) / speed_mps
                    if alternative["reachable"] and alternative["edge_ids"] else None
                )
                use_replan = (
                    alternative_eta is not None and alternative_eta < restricted_eta
                )
                selected_route = alternative if use_replan else original
                selected_eta = alternative_eta if use_replan else restricted_eta
                action_type = (
                    "weather_safe_route_replan" if use_replan
                    else "weather_speed_restriction"
                )
                selected_edges = list(selected_route["edge_ids"])
                decisions.append({
                    "task_id": task_id, "vehicle_id": assignment.vehicle_id,
                    "action_type": action_type,
                    "degraded_edge_id": degraded_edge_id,
                    "baseline_eta_s": round(baseline_eta, 6),
                    "restricted_route_eta_s": round(restricted_eta, 6),
                    "alternative_route_eta_s": (
                        round(alternative_eta, 6)
                        if alternative_eta is not None else None
                    ),
                    "selected_eta_s": round(float(selected_eta), 6),
                    "estimated_delay_s": round(
                        float(selected_eta) - baseline_eta, 6
                    ),
                    "restricted_speed_factor": speed_factor,
                    "original_edge_ids": original["edge_ids"],
                    "selected_edge_ids": selected_edges,
                    "original_distance_m": original["distance_m"],
                    "selected_distance_m": selected_route["distance_m"],
                    "route_contract": selected_route,
                    "eta_source": "route_length_divided_by_configured_target_speed",
                    "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
                    "constraint_results": {
                        "selected_route_available": bool(selected_edges),
                        "weather_control_respected": (
                            degraded_edge_id not in selected_edges
                            if use_replan else 0.0 < speed_factor <= 1.0
                        ),
                        "eta_available": selected_eta is not None,
                        "vehicle_assignment_retained": True,
                    },
                    "policy_version": POLICY_VERSION,
                })
                route_plans.append({
                    "route_plan_id": "{}:weather-response".format(task_id),
                    "vehicle_id": assignment.vehicle_id, "task_id": task_id,
                    "planner_version": POLICY_VERSION,
                    "start_node": start_id, "goal_node": goal_id,
                    "distance_m": selected_route["distance_m"],
                    "status": "planned", "replan_reason": action_type,
                    "edge_ids": selected_route["edge_ids"],
                    "route_contract": selected_route,
                })

        for task in tasks:
            task.status = "completed"
            task.completed_tick = parameters["recovery_tick"]
            task.status_reason = (
                "structural_mock_completion_after_weather_response"
                if task.task_id in affected_task_ids
                else "structural_mock_completion_unaffected"
            )
        adapter.complete_tasks(tasks)
        replanned = sum(
            item["action_type"] == "weather_safe_route_replan"
            for item in decisions
        )
        return {
            "status": "PASS", "mode": "mock_structural",
            "simulation_claim": (
                "parameterized_weather_and_eta_surrogate_only_no_carla_physics"
            ),
            "scenario_id": dynamic.scenario_id, "seed": effective_seed,
            "scenario_source": "map_resources_topology_and_global_p5",
            "random_mode": "seeded_structural_extreme_weather_mock_only",
            "weather_event": {
                "event_type": "extreme_rainfall_road_capacity_degradation",
                "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                **parameters,
            },
            "degraded_edge_id": degraded_edge_id,
            "road_status_during_event": "RESTRICTED",
            "road_status_after_recovery": "OPEN",
            "weather_status_after_recovery": "clear",
            "route_impact_task_ids": affected_task_ids,
            "affected_task_count": len(affected_task_ids),
            "unaffected_task_count": len(tasks) - len(affected_task_ids),
            "replanned_task_count": replanned,
            "speed_restricted_task_count": len(decisions) - replanned,
            "weather_decisions": decisions,
            "route_plans": route_plans,
            "route_planner_version": POLICY_VERSION,
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
                "P5/topology facts plus parameterized synthetic weather and ETA "
                "surrogates; no CARLA physical, tire-road, collision or real-mine validation."
            ),
        }
    finally:
        adapter.close()
