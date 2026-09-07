"""Offline structural runner for selective road-closure response."""
from copy import deepcopy
from datetime import datetime, timezone
from random import Random
from typing import Any, Dict, Optional, Set, Tuple

from ..adapters.mock_adapter import MockAdapter
from ..map_resources import MapResourceStore, RoadGraph, RoutePlanner
from ..map_resources.road_graph import (
    identify_affected_routes, route_plans_from_store,
    verified_point_anchors_from_store,
)
from ..scheduler import BaselineScheduler, tasks_from_zones
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot
from .road_state import RoadState
from .random_s01 import prepare_random_map_workload


def run_s07_structural_mock(
    config: Any,
    seed: Optional[int] = None,
    run_store: Any = None,
    map_resource_store: Any = None,
    map_id: Optional[str] = None,
    resource_version: Optional[str] = None,
    route_endpoints: Optional[Dict[str, Any]] = None,
    closed_edge_id: Optional[str] = None,
) -> Dict[str, Any]:
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    map_config = getattr(config, "map_resource", None)
    map_id = map_id or (map_config.map_id if map_config else None)
    resource_version = resource_version or (map_config.resource_version if map_config else None)
    route_endpoints = route_endpoints or (map_config.route_endpoints if map_config else None)
    closed_edge_id = closed_edge_id or (map_config.closed_edge_id if map_config else "R1")
    owned_map_store = False
    if map_resource_store is None and map_config and map_config.database_path and map_config.database_path.is_file():
        map_resource_store = MapResourceStore(map_config.database_path)
        owned_map_store = True

    episode = build_episode(config, run_id="s07-structural-mock", seed=effective_seed)
    tasks = tasks_from_zones(config.zones)
    adapter = MockAdapter(config)
    scheduler = BaselineScheduler()
    road_state = RoadState({closed_edge_id: "OPEN"})
    if run_store is not None:
        run_store.record_episode(episode)
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        initial = scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
        initial_task_states = [task.to_dict() for task in tasks]
        route_source = "synthetic_fixture"
        route_plans = {}
        if map_resource_store is not None and map_id and resource_version and route_endpoints:
            route_plans = route_plans_from_store(
                map_resource_store, map_id, resource_version, route_endpoints
            )
            if route_plans:
                route_source = "map_resource_db"
        if not route_plans:
            route_plans = {
                item.task_id: ([closed_edge_id] if item.vehicle_id == "haul_vehicle_02" else ["R2"])
                for item in initial
            }
        affected_task_ids = identify_affected_routes(route_plans, {closed_edge_id})
        affected = next(item for item in initial if item.task_id in affected_task_ids)
        road_state.close(closed_edge_id)
        stamp = datetime.now(timezone.utc).isoformat()
        if run_store is not None:
            run_store.record_event(episode.run_id, stamp, "road_closed", {"tick": 30, "road_id": closed_edge_id, "status": "CLOSED"})
        target = next(task for task in tasks if task.task_id == affected.task_id)
        original_vehicle = target.assigned_vehicle_id
        target.assigned_vehicle_id = None
        target.status = "pending"
        target.status_reason = "released_after_road_closure"
        states = list(adapter.list_states())
        replanned = scheduler.assign([target], states, config.zones, excluded_vehicle_ids={original_vehicle})
        adapter.dispatch(tasks, config.zones)
        if run_store is not None:
                run_store.record_event(episode.run_id, stamp, "task_replanned", {"tick": 30, "task_id": target.task_id, "vehicle_id": replanned[0].vehicle_id, "reason": "road_closure"})
        replanned_vehicle_states = vehicle_state_snapshot(adapter.list_states())
        for task in tasks:
            task.status = "completed"
            task.completed_tick = 31
            task.status_reason = "structural_mock_completion_after_road_replan" if task.task_id == target.task_id else "structural_mock_completion"
            if run_store is not None:
                run_store.record_event(episode.run_id, stamp, "task_completed", {"tick": 31, "task_id": task.task_id, "status": task.status, "reason": task.status_reason})
        adapter.complete_tasks(tasks)
        road_state.reopen(closed_edge_id)
        if run_store is not None:
            run_store.record_event(episode.run_id, stamp, "road_reopened", {"tick": 31, "road_id": closed_edge_id, "status": "OPEN"})
        return {
            "status": "PASS", "mode": "mock_structural",
            "simulation_claim": "selective_road_closure_replan_only_no_carla_physics",
            "scenario_id": config.scenario_id, "seed": effective_seed,
            "closed_road_id": closed_edge_id, "affected_task_id": target.task_id,
            "route_impact_task_ids": affected_task_ids, "route_source": route_source,
            "initial_assignment_count": len(initial), "replanned_task_count": len(replanned),
            "unaffected_task_count": len(tasks) - 1,
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "road_status_after_reopen": road_state.statuses[closed_edge_id],
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_vehicle_states": initial_vehicle_states,
            "initial_task_states": initial_task_states,
            "replanned_vehicle_states": replanned_vehicle_states,
            "final_vehicle_states": vehicle_state_snapshot(adapter.list_states()),
        }
    finally:
        adapter.close()
        if owned_map_store:
            map_resource_store.close()


def run_random_s07_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   scenario_key: str = "s07",
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Run a seeded road-edge closure and selective route-replanning episode.

    The interruption is admitted only when it affects part of the active
    fleet and every affected original vehicle has a topology alternative.
    This validates orchestration and graph logic, never CARLA traversal.
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
            "S07 has no topology-consistent route candidates; run "
            "./map_resources.sh build-route-candidates"
        )
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m,
        scenario_key,
        eligible_pairs=eligible_pairs,
    )
    dynamic = workload["config"]
    effective_seed = workload["seed"]
    randomization = dynamic.scenario_variables.get("randomization", {})
    events = randomization.get("events", []) if isinstance(randomization, dict) else []
    event_parameters = (
        events[0].get("parameters", {})
        if events and isinstance(events[0], dict) else {}
    )
    maximum_detour_ratio = float(event_parameters.get("maximum_detour_ratio", 4.0))
    if maximum_detour_ratio < 1.0:
        raise ValueError("S07 maximum_detour_ratio must be at least 1.0")
    tasks = deepcopy(workload["tasks"])
    vehicle_origins = workload["vehicle_origins"]
    zone_targets = workload["zone_targets"]
    route_costs = workload["route_costs"]
    binding = dynamic.map_resource

    def route_distance(vehicle, zone):
        value = route_costs.get(
            (vehicle_origins[vehicle.vehicle_id], zone_targets[zone.zone_id])
        )
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
    episode = build_episode(dynamic, run_id="s07-random-structural-mock",
                            seed=effective_seed)
    adapter = MockAdapter(dynamic)
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        candidate_rankings = {
            task.task_id: [item.to_dict() for item in scheduler.rank_candidates(
                task, states, dynamic.zones
            )]
            for task in tasks
        }
        assignments = scheduler.assign(tasks, states, dynamic.zones)
        adapter.dispatch(tasks, dynamic.zones)
        initial_task_states = [task.to_dict() for task in tasks]
        endpoints = {
            item.task_id: (
                vehicle_origins[item.vehicle_id], zone_targets[item.task_id]
            )
            for item in assignments
        }
        with MapResourceStore(binding.database_path) as store:
            normal_plans = route_plans_from_store(
                store, binding.map_id, binding.resource_version, endpoints
            )
            if len(normal_plans) != len(assignments):
                raise ValueError(
                    "S07 requires topology-derived candidates for every active task; "
                    "run ./map_resources.sh build-route-candidates"
                )
            graph = RoadGraph.from_store(
                store, binding.map_id, binding.resource_version
            )
            route_planner = RoutePlanner(graph)
            anchors = verified_point_anchors_from_store(
                store, graph, binding.map_id
            )

            admitted = []
            assignment_by_task = {item.task_id: item for item in assignments}
            task_by_id = {task.task_id: task for task in tasks}
            internal_edges = sorted({
                edge_id for plan in normal_plans.values() for edge_id in plan[1:-1]
            })
            for edge_id in internal_edges:
                impacted = sorted(
                    task_id for task_id, plan in normal_plans.items()
                    if edge_id in plan
                )
                if not impacted or len(impacted) >= len(normal_plans):
                    continue
                # The closure must be ahead of, rather than under, every
                # affected vehicle and must permit deterministic graph bypass.
                if any(normal_plans[task_id][0] == edge_id
                       or normal_plans[task_id][-1] == edge_id
                       for task_id in impacted):
                    continue
                solutions = {}
                reserved_takeover_vehicles = set()
                impacted_vehicle_ids = {
                    assignment_by_task[task_id].vehicle_id for task_id in impacted
                }
                for task_id in impacted:
                    start_id, goal_id = endpoints[task_id]
                    start, goal = anchors.get(start_id), anchors.get(goal_id)
                    if start is None or goal is None:
                        break
                    alternative = route_planner.plan(
                        start, goal, closed_edge_ids={edge_id}
                    ).to_dict()
                    original = route_planner.plan(start, goal).to_dict()
                    original_replan_ok = (
                        alternative["reachable"] and alternative["edge_ids"]
                        and original["distance_m"] is not None
                        and float(original["distance_m"]) > 0
                        and float(alternative["distance_m"])
                        <= maximum_detour_ratio * float(original["distance_m"])
                    )
                    if original_replan_ok:
                        solutions[task_id] = {
                            "action_type": "route_replan_same_vehicle",
                            "vehicle_id": assignment_by_task[task_id].vehicle_id,
                            "start_point_id": start_id,
                            "route": alternative,
                            "candidate_evaluations": [],
                        }
                        continue

                    # If the original vehicle cannot bypass the closure, select
                    # a capable vehicle outside the affected set whose route
                    # starts on a still-connected side of the road graph.
                    takeover_candidates = []
                    required = set(task_by_id[task_id].required_capabilities)
                    for candidate in dynamic.vehicles:
                        if (candidate.vehicle_id in impacted_vehicle_ids
                                or candidate.vehicle_id in reserved_takeover_vehicles
                                or not required.issubset(set(candidate.capabilities))):
                            continue
                        candidate_start_id = vehicle_origins[candidate.vehicle_id]
                        if (candidate_start_id, goal_id) not in eligible_pairs:
                            continue
                        candidate_start = anchors.get(candidate_start_id)
                        if candidate_start is None:
                            continue
                        baseline = route_planner.plan(
                            candidate_start, goal
                        ).to_dict()
                        takeover_route = route_planner.plan(
                            candidate_start, goal, closed_edge_ids={edge_id}
                        ).to_dict()
                        if (not takeover_route["reachable"]
                                or not takeover_route["edge_ids"]
                                or baseline["distance_m"] is None
                                or float(baseline["distance_m"]) <= 0
                                or float(takeover_route["distance_m"])
                                > maximum_detour_ratio * float(baseline["distance_m"])):
                            continue
                        takeover_candidates.append({
                            "vehicle_id": candidate.vehicle_id,
                            "start_point_id": candidate_start_id,
                            "route": takeover_route,
                            "score": float(takeover_route["distance_m"]),
                            "constraint_results": {
                                "capability_match": True,
                                "outside_affected_vehicle_set": True,
                                "topology_consistent_pair": True,
                                "closed_edge_avoided": True,
                                "detour_ratio_allowed": True,
                            },
                        })
                    takeover_candidates.sort(
                        key=lambda item: (item["score"], item["vehicle_id"])
                    )
                    if not takeover_candidates:
                        break
                    selected = takeover_candidates[0]
                    reserved_takeover_vehicles.add(selected["vehicle_id"])
                    solutions[task_id] = {
                        "action_type": "task_takeover_after_no_safe_bypass",
                        "vehicle_id": selected["vehicle_id"],
                        "start_point_id": selected["start_point_id"],
                        "route": selected["route"],
                        "candidate_evaluations": takeover_candidates,
                    }
                if len(solutions) == len(impacted):
                    admitted.append((edge_id, impacted, solutions))
            if not admitted:
                raise ValueError(
                    "seeded S07 workload has no selective road closure with a safe topology bypass"
                )
            closed_edge_id, affected_task_ids, solutions = Random(
                effective_seed + 7007
            ).choice(admitted)

            road_state = RoadState({closed_edge_id: "OPEN"})
            road_state.close(closed_edge_id)
            route_changes = []
            route_plan_records = []
            for task_id in sorted(normal_plans):
                assignment = assignment_by_task[task_id]
                start_id, goal_id = endpoints[task_id]
                original = route_planner.plan(
                    anchors[start_id], anchors[goal_id]
                ).to_dict()
                route_plan_records.append({
                    "route_plan_id": "{}:original".format(task_id),
                    "vehicle_id": assignment.vehicle_id,
                    "task_id": task_id,
                    "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                    "start_node": start_id,
                    "goal_node": goal_id,
                    "distance_m": original["distance_m"],
                    "status": "superseded" if task_id in affected_task_ids else "unchanged",
                    "replan_reason": "road_edge_closed" if task_id in affected_task_ids else None,
                    "edge_ids": original["edge_ids"],
                    "route_contract": original,
                })
                if task_id not in affected_task_ids:
                    continue
                solution = solutions[task_id]
                alternative = solution["route"]
                selected_vehicle_id = solution["vehicle_id"]
                if solution["action_type"] == "task_takeover_after_no_safe_bypass":
                    task = task_by_id[task_id]
                    task.original_vehicle_id = assignment.vehicle_id
                    task.assigned_vehicle_id = selected_vehicle_id
                    task.handover_reason = "original_vehicle_has_no_safe_road_bypass"
                    task.handover_tick = 30
                    task.transfer_count += 1
                route_changes.append({
                    "task_id": task_id,
                    "original_vehicle_id": assignment.vehicle_id,
                    "vehicle_id": selected_vehicle_id,
                    "action_type": solution["action_type"],
                    "original_edge_ids": list(normal_plans[task_id]),
                    "replanned_edge_ids": list(alternative["edge_ids"]),
                    "original_distance_m": original["distance_m"],
                    "replanned_distance_m": alternative["distance_m"],
                    "closed_edge_id": closed_edge_id,
                    "takeover_required": selected_vehicle_id != assignment.vehicle_id,
                    "candidate_evaluations": solution["candidate_evaluations"],
                    "route_contract": alternative,
                    "constraint_results": {
                        "selected_route_reachable": bool(alternative.get("reachable")),
                        "selected_route_nonempty": bool(alternative.get("edge_ids")),
                        "closed_edge_avoided": (
                            closed_edge_id not in alternative.get("edge_ids", [])
                        ),
                        "detour_ratio_allowed": (
                            original.get("distance_m") is not None
                            and float(original["distance_m"]) > 0
                            and float(alternative["distance_m"])
                            <= maximum_detour_ratio * float(original["distance_m"])
                        ),
                    },
                })
                route_plan_records.append({
                    "route_plan_id": "{}:replanned".format(task_id),
                    "vehicle_id": selected_vehicle_id,
                    "task_id": task_id,
                    "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                    "start_node": solution["start_point_id"],
                    "goal_node": goal_id,
                    "distance_m": alternative["distance_m"],
                    "status": "planned",
                    "replan_reason": "road_edge_closed",
                    "edge_ids": alternative["edge_ids"],
                    "closed_edge_ids": [closed_edge_id],
                    "route_contract": alternative,
                })

        adapter.dispatch(tasks, dynamic.zones)
        action_by_task = {
            item["task_id"]: item["action_type"] for item in route_changes
        }
        for task in tasks:
            task.status = "completed"
            task.completed_tick = 31
            task.status_reason = (
                "structural_mock_completion_after_takeover"
                if action_by_task.get(task.task_id) == "task_takeover_after_no_safe_bypass"
                else "structural_mock_completion_after_same_vehicle_replan"
                if task.task_id in affected_task_ids
                else "structural_mock_completion_unaffected"
            )
        adapter.complete_tasks(tasks)
        road_state.reopen(closed_edge_id)
        return {
            "status": "PASS",
            "mode": "mock_structural",
            "simulation_claim": "map_backed_route_replan_or_takeover_only_no_carla_physics",
            "scenario_id": dynamic.scenario_id,
            "seed": effective_seed,
            "scenario_source": "map_resources_topology_and_global_p5",
            "random_mode": "seeded_structural_road_closure_mock_only",
            "closed_edge_id": closed_edge_id,
            "closure_candidate_count": len(admitted),
            "maximum_admitted_detour_ratio": maximum_detour_ratio,
            "route_source": "map_resource_db",
            "route_impact_task_ids": affected_task_ids,
            "affected_task_id": affected_task_ids[0],
            "affected_task_count": len(affected_task_ids),
            "unaffected_task_count": len(tasks) - len(affected_task_ids),
            "replanned_task_count": len(route_changes),
            "same_vehicle_replan_count": sum(
                item["action_type"] == "route_replan_same_vehicle"
                for item in route_changes
            ),
            "takeover_count": sum(
                item["action_type"] == "task_takeover_after_no_safe_bypass"
                for item in route_changes
            ),
            "takeover_assignments": [
                {
                    "task_id": item["task_id"],
                    "original_vehicle_id": item["original_vehicle_id"],
                    "vehicle_id": item["vehicle_id"],
                    "reason": "original_vehicle_has_no_safe_road_bypass",
                }
                for item in route_changes if item["takeover_required"]
            ],
            "route_changes": route_changes,
            "route_plans": route_plan_records,
            "route_planner_version": "RoadGraph-Dijkstra-Topology-V1",
            "road_status_after_reopen": road_state.statuses[closed_edge_id],
            "initial_assignment_count": len(assignments),
            "assignment_count": len(assignments),
            "assignments": [item.to_dict() for item in assignments],
            "candidate_rankings": candidate_rankings,
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "task_count": len(tasks),
            "tasks": [task.to_dict() for task in tasks],
            "map_resource_task_draft": workload["task_drafts"],
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_vehicle_states": initial_vehicle_states,
            "initial_task_states": initial_task_states,
            "final_vehicle_states": vehicle_state_snapshot(adapter.list_states()),
            "boundary": (
                "P5 strict reachability plus topology-derived edge bypass; no "
                "CARLA physical driving, collision, clearance or fleet validation."
            ),
        }
    finally:
        adapter.close()
