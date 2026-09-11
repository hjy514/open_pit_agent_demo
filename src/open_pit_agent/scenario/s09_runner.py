"""Compound road-closure plus vehicle-failure structural scenario."""
from copy import deepcopy
from random import Random
from typing import Any, Dict, Optional, Set, Tuple

from ..map_resources import MapResourceStore, RoadGraph, RoutePlanner
from ..map_resources.road_graph import verified_point_anchors_from_store
from .generator import ROLE_DETAILS, sample_event_timing
from .s07_runner import run_random_s07_structural_mock


POLICY_VERSION = "compound-road-fault-safe-takeover-v1"


def _event_parameters(config: Any, seed: int) -> Dict[str, int]:
    timing = sample_event_timing(config, "s09", seed)
    road_tick = timing["road_closure_tick"]
    failure_tick = timing["vehicle_failure_tick"]
    recovery_tick = timing["recovery_tick"]
    if not road_tick < failure_tick < recovery_tick:
        raise ValueError(
            "S09 requires road_closure_tick < vehicle_failure_tick < recovery_tick"
        )
    return {
        "road_closure_tick": road_tick,
        "vehicle_failure_tick": failure_tick,
        "recovery_tick": recovery_tick,
    }


def run_random_s09_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   _generation_attempt: int = 0,
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Resolve two ordered events against one task/fleet/map state."""
    requested_seed = config.demo.random_seed if seed is None else int(seed)
    workload_seed = requested_seed + int(_generation_attempt)
    try:
        result = run_random_s07_structural_mock(
            config, seed=workload_seed, vehicle_count=vehicle_count,
            minimum_length_m=minimum_length_m,
            maximum_length_m=maximum_length_m, scenario_key="s09",
            eligible_pairs_override=eligible_pairs_override,
        )
    except ValueError as exc:
        if _generation_attempt < 19:
            return run_random_s09_structural_mock(
                config, seed=requested_seed, vehicle_count=vehicle_count,
                minimum_length_m=minimum_length_m,
                maximum_length_m=maximum_length_m,
                _generation_attempt=_generation_attempt + 1,
                eligible_pairs_override=eligible_pairs_override,
            )
        raise ValueError(
            "S09 could not generate a P6-admitted selective road event after "
            "20 deterministic attempts: {}".format(exc)
        ) from exc
    result = deepcopy(result)
    effective_seed = int(result["seed"])
    parameters = _event_parameters(config, requested_seed)
    binding = config.map_resource
    closed_edge_id = result["closed_edge_id"]
    road_affected_tasks = set(result.get("route_impact_task_ids", []))
    drafts = result.get("map_resource_task_draft", [])
    vehicle_origins = {
        str(item["vehicle_id"]): str(item["from_point_id"])
        for item in drafts
    }
    task_targets = {
        str(item["task_id"]): str(item["to_point_id"])
        for item in drafts
    }
    tasks = {
        str(item["task_id"]): item for item in result.get("tasks", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    fleet_roles = {
        str(item["vehicle_id"]): str(item.get("role"))
        for item in result.get("fleet", {}).get("vehicles", [])
    }
    current_task_by_vehicle = {
        str(task.get("assigned_vehicle_id")): task_id
        for task_id, task in tasks.items() if task.get("assigned_vehicle_id")
    }

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
            eligible_pairs = set(eligible_pairs_override)
        graph = RoadGraph.from_store(store, binding.map_id,
                                     binding.resource_version)
        route_planner = RoutePlanner(graph)
        anchors = verified_point_anchors_from_store(store, graph, binding.map_id)
        feasible_failures = []
        for task_id in sorted(tasks):
            task = tasks[task_id]
            # Keep the two incident stages distinct.  The vehicle-failure
            # stage must not target work that the preceding road closure has
            # already rerouted or transferred.
            if task_id in road_affected_tasks:
                continue
            failed_vehicle_id = str(task.get("assigned_vehicle_id") or "")
            if not failed_vehicle_id or failed_vehicle_id not in vehicle_origins:
                continue
            required = set(task.get("required_capabilities", []))
            goal_id = task_targets.get(task_id)
            if goal_id not in anchors:
                continue
            candidates = []
            for candidate_id in sorted(vehicle_origins):
                if candidate_id == failed_vehicle_id:
                    continue
                candidate_task = current_task_by_vehicle.get(candidate_id)
                if candidate_task in road_affected_tasks:
                    continue
                role = fleet_roles.get(candidate_id)
                role_detail = ROLE_DETAILS.get(role)
                capabilities = set(role_detail[2]) if role_detail else set()
                start_id = vehicle_origins[candidate_id]
                constraints = {
                    "vehicle_not_failed": candidate_id != failed_vehicle_id,
                    "capability_match": required.issubset(capabilities),
                    "candidate_not_held_by_road_event": (
                        candidate_task not in road_affected_tasks
                    ),
                    # The response vehicle is selected from work already
                    # heading to the same service target.  Its original task
                    # therefore remains valid after the urgent takeover and
                    # no unvalidated cross-map recovery leg is invented.
                    "candidate_route_aligned_with_failed_goal": (
                        bool(candidate_task)
                        and task_targets.get(candidate_task) == goal_id
                    ),
                    "p5_pair_available": (start_id, goal_id) in eligible_pairs,
                    "closed_edge_avoided": False,
                }
                if not all(value for key, value in constraints.items()
                           if key != "closed_edge_avoided"):
                    continue
                route = route_planner.plan(
                    anchors[start_id], anchors[goal_id],
                    closed_edge_ids={closed_edge_id},
                ).to_dict()
                constraints["closed_edge_avoided"] = (
                    bool(route.get("reachable"))
                    and bool(route.get("edge_ids"))
                    and closed_edge_id not in route.get("edge_ids", [])
                )
                if not all(constraints.values()):
                    continue
                # The aligned candidate's original task has the same service
                # goal, so completing the takeover also places it at its own
                # destination.  Record that zero-length recovery explicitly;
                # it is not a fabricated physical route.
                displaced_task_goal_id = task_targets.get(candidate_task)
                displaced_route = None
                displaced_reachable = (
                    bool(candidate_task)
                    and displaced_task_goal_id == goal_id
                )
                displaced_avoids_closure = displaced_reachable
                constraints.update({
                    "displaced_task_recovery_reachable": (
                        displaced_reachable
                    ),
                    "displaced_route_avoids_closure": (
                        displaced_avoids_closure
                    ),
                })
                if not all(constraints.values()):
                    continue
                takeover_distance = float(route["distance_m"])
                displaced_distance = float(
                    displaced_route.get("distance_m") or 0.0
                ) if displaced_route else 0.0
                candidates.append({
                    "vehicle_id": candidate_id,
                    "candidate_current_task_id": candidate_task,
                    "start_point_id": start_id,
                    "goal_point_id": goal_id,
                    "route_distance_m": float(route["distance_m"]),
                    "route_edge_ids": list(route["edge_ids"]),
                    "route_contract": route,
                    "constraint_results": constraints,
                    "displaced_task_goal_point_id": (
                        displaced_task_goal_id
                    ),
                    "displaced_task_recovery_route_contract": (
                        displaced_route
                    ),
                    "displaced_task_recovery_distance_m": (
                        displaced_distance
                    ),
                    "score": takeover_distance + displaced_distance,
                    "cost_components": {
                        "takeover_route_distance_m": takeover_distance,
                        "displaced_task_recovery_distance_m": (
                            displaced_distance
                        ),
                    },
                    "score_source": (
                        "safe_aligned_route_takeover"
                    ),
                })
            candidates.sort(key=lambda item: (item["score"], item["vehicle_id"]))
            if candidates:
                feasible_failures.append({
                    "task_id": task_id,
                    "failed_vehicle_id": failed_vehicle_id,
                    "goal_point_id": goal_id,
                    "candidates": candidates,
                })
    if not feasible_failures:
        if _generation_attempt < 19:
            return run_random_s09_structural_mock(
                config, seed=requested_seed, vehicle_count=vehicle_count,
                minimum_length_m=minimum_length_m,
                maximum_length_m=maximum_length_m,
                _generation_attempt=_generation_attempt + 1,
                eligible_pairs_override=eligible_pairs_override,
            )
        raise ValueError(
            "S09 could not generate a feasible compound event after 20 "
            "deterministic attempts; no hard constraint was relaxed"
        )
    failure = Random(effective_seed + 9090).choice(feasible_failures)
    selected = failure["candidates"][0]
    failed_task_id = failure["task_id"]
    failed_vehicle_id = failure["failed_vehicle_id"]
    selected_vehicle_id = selected["vehicle_id"]
    task = tasks[failed_task_id]
    task.update({
        "original_vehicle_id": failed_vehicle_id,
        "assigned_vehicle_id": selected_vehicle_id,
        "handover_reason": "vehicle_fault_during_active_road_closure",
        "handover_tick": parameters["vehicle_failure_tick"],
        "transfer_count": int(task.get("transfer_count") or 0) + 1,
        "completed_tick": parameters["recovery_tick"],
        "status_reason": "structural_mock_completion_after_compound_takeover",
    })
    for item in tasks.values():
        item["completed_tick"] = parameters["recovery_tick"]

    final_vehicle_states = deepcopy(result.get("final_vehicle_states", []))
    for state in final_vehicle_states:
        if state.get("vehicle_id") == failed_vehicle_id:
            state.update({
                "health": "fault",
                "available": False,
                "task_status": "failed_isolated",
                "current_task_id": None,
            })

    takeover_decision = {
        "task_id": failed_task_id,
        "action_type": "task_takeover_during_road_closure",
        "failed_vehicle_id": failed_vehicle_id,
        "selected_vehicle_id": selected_vehicle_id,
        "selected_vehicle_current_task_id": selected.get(
            "candidate_current_task_id"),
        "goal_point_id": failure["goal_point_id"],
        "route_distance_m": selected["route_distance_m"],
        "route_edge_ids": selected["route_edge_ids"],
        "route_contract": selected["route_contract"],
        "displaced_task_goal_point_id": selected.get(
            "displaced_task_goal_point_id"
        ),
        "displaced_task_recovery_route_contract": selected.get(
            "displaced_task_recovery_route_contract"
        ),
        "displaced_task_recovery_distance_m": selected.get(
            "displaced_task_recovery_distance_m"
        ),
        "cost_components": selected.get("cost_components", {}),
        "closed_edge_id": closed_edge_id,
        "candidate_evaluations": failure["candidates"],
        "constraint_results": selected["constraint_results"],
        "policy_version": POLICY_VERSION,
        "measurement_status": "STRUCTURAL_ROUTE_LOGIC_NOT_CARLA_MEASURED",
    }
    result["route_plans"].append({
        "route_plan_id": "{}:compound-fault-takeover".format(failed_task_id),
        "vehicle_id": selected_vehicle_id, "task_id": failed_task_id,
        "planner_version": "RoadGraph-Dijkstra-Topology-V1",
        "start_node": selected["start_point_id"],
        "goal_node": selected["goal_point_id"],
        "distance_m": selected["route_distance_m"], "status": "planned",
        "replan_reason": "vehicle_fault_during_active_road_closure",
        "edge_ids": selected["route_edge_ids"],
        "closed_edge_ids": [closed_edge_id],
        "route_contract": selected["route_contract"],
    })
    road_takeovers = int(result.get("takeover_count") or 0)
    compound_task_ids = sorted(road_affected_tasks.union({failed_task_id}))
    result.update({
        "scenario_id": "s09-{}v-seed-{}".format(
            vehicle_count, requested_seed),
        "seed": requested_seed,
        "workload_seed": effective_seed,
        "generation_attempt": int(_generation_attempt),
        "scenario_admission_status": "COMPOUND_FEASIBLE",
        "simulation_claim": (
            "ordered_road_closure_and_vehicle_failure_logic_only_no_carla_physics"
        ),
        "scenario_source": "single_map_resource_workload_with_ordered_compound_events",
        "random_mode": "seeded_structural_compound_mock_only",
        "compound_profile": "road_closure_then_vehicle_failure_v1",
        "compound_events": [
            {
                "event_type": "road_closure",
                "tick": parameters["road_closure_tick"],
                "edge_id": closed_edge_id,
                "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
            },
            {
                "event_type": "vehicle_failure",
                "tick": parameters["vehicle_failure_tick"],
                "recovery_tick": parameters["recovery_tick"],
                "vehicle_id": failed_vehicle_id,
                "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
            },
        ],
        "compound_event_parameters": parameters,
        "failed_vehicle_id": failed_vehicle_id,
        "closure_tick": parameters["road_closure_tick"],
        "failure_tick": parameters["vehicle_failure_tick"],
        "recovery_tick": parameters["recovery_tick"],
        "released_task_ids": [failed_task_id],
        "reassignment_count": 1,
        "compound_failure_decisions": [takeover_decision],
        "road_affected_task_count": len(road_affected_tasks),
        "road_takeover_count": road_takeovers,
        "compound_affected_task_ids": compound_task_ids,
        "compound_affected_task_count": len(compound_task_ids),
        "unaffected_task_count": len(tasks) - len(compound_task_ids),
        "road_status_after_reopen": "OPEN",
        "failed_vehicle_status_after_run": "FAILED_ISOLATED",
        "policy_version": POLICY_VERSION,
        "tasks": [tasks[key] for key in sorted(tasks)],
        "final_vehicle_states": final_vehicle_states,
        "takeover_count": road_takeovers + 1,
        "boundary": (
            "Two parameterized events applied to one seeded map/fleet/task "
            "state using P5/topology constraints; no CARLA physical driving, "
            "collision, measured latency or real-mine incident data."
        ),
    })
    return result
