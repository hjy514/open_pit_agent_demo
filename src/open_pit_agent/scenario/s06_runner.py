"""Seeded shared-road congestion and safe-entry scheduling scenario."""
from copy import deepcopy
from random import Random
from typing import Any, Dict, Optional, Set, Tuple

from ..adapters.mock_adapter import MockAdapter
from ..map_resources import MapResourceStore, RoadGraph
from ..map_resources.road_graph import route_plans_from_store
from ..scheduler import BaselineScheduler
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot
from .generator import sample_event_timing
from .random_s01 import prepare_random_map_workload


POLICY_VERSION = "shared-road-capacity-scheduler-v1"


def _traffic_parameters(config: Any, seed: int) -> Dict[str, float]:
    randomization = config.scenario_variables.get("randomization", {})
    events = randomization.get("events", []) if isinstance(randomization, dict) else []
    raw = events[0].get("parameters", {}) if events else {}
    blockage_range = raw.get("initial_blockage_seconds_range", [15.0, 35.0])
    if (not isinstance(blockage_range, list) or len(blockage_range) != 2
            or float(blockage_range[0]) < 0
            or float(blockage_range[0]) > float(blockage_range[1])):
        raise ValueError("S06 initial_blockage_seconds_range is invalid")
    capacity = int(raw.get("road_capacity_vehicles", 1))
    headway = float(raw.get("minimum_safety_headway_seconds", 8.0))
    if capacity != 1:
        raise ValueError("S06 V1 currently requires road_capacity_vehicles=1")
    if headway < 0:
        raise ValueError("S06 minimum_safety_headway_seconds must be non-negative")
    random = Random(int(seed) + 6006)
    timing = sample_event_timing(config, "s06", seed)
    return {
        **timing,
        "road_capacity_vehicles": capacity,
        "minimum_safety_headway_seconds": headway,
        "initial_blockage_seconds": round(random.uniform(
            float(blockage_range[0]), float(blockage_range[1])
        ), 6),
    }


def run_random_s06_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Resolve a reproducible capacity-one shared-road queue without CARLA."""
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
    physical_override = (
        set(eligible_pairs_override)
        if eligible_pairs_override is not None else None
    )
    if physical_override is not None:
        eligible_pairs = physical_override
    if not eligible_pairs:
        raise ValueError(
            "S06 has no topology-consistent route candidates; run "
            "./map_resources.sh build-route-candidates"
        )
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m,
        "s06", eligible_pairs=eligible_pairs,
    )
    dynamic = workload["config"]
    effective_seed = workload["seed"]
    parameters = _traffic_parameters(config, effective_seed)
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
    episode = build_episode(dynamic, run_id="s06-random-structural-mock",
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
        task_by_id = {item.task_id: item for item in tasks}
        vehicle_by_id = {item.vehicle_id: item for item in dynamic.vehicles}

        with MapResourceStore(binding.database_path) as store:
            plans = route_plans_from_store(
                store, binding.map_id, binding.resource_version, endpoints,
                physically_reached_pairs=physical_override,
            )
            if len(plans) != len(assignments):
                raise ValueError("S06 requires a topology route for every active task")
            graph = RoadGraph.from_store(store, binding.map_id,
                                         binding.resource_version)
            shared_edges = []
            for edge_id in sorted(graph.edges):
                impacted = sorted(
                    task_id for task_id, route in plans.items()
                    if edge_id in route[1:-1]
                )
                if 2 <= len(impacted) < len(plans):
                    shared_edges.append((edge_id, impacted))
            if not shared_edges:
                raise ValueError(
                    "S06 workload has no selectively shared internal road segment"
                )
            bottleneck_edge_id, affected_task_ids = Random(
                effective_seed + 6060
            ).choice(shared_edges)
            edge_length = float(graph.edges[bottleneck_edge_id].length_m)

            queue = []
            route_plan_records = []
            for task_id in sorted(plans):
                assignment = assignment_by_task[task_id]
                vehicle = vehicle_by_id[assignment.vehicle_id]
                speed_mps = float(vehicle.target_speed_kmh) / 3.6
                route = plans[task_id]
                route_distance_m = float(route_costs[
                    (vehicle_origins[assignment.vehicle_id], zone_targets[task_id])
                ])
                route_plan_records.append({
                    "route_plan_id": "{}:traffic-baseline".format(task_id),
                    "vehicle_id": assignment.vehicle_id, "task_id": task_id,
                    "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                    "start_node": endpoints[task_id][0],
                    "goal_node": endpoints[task_id][1],
                    "distance_m": route_distance_m, "status": "active",
                    "edge_ids": route,
                })
                if task_id not in affected_task_ids:
                    continue
                edge_index = route.index(bottleneck_edge_id)
                distance_to_edge = min(
                    route_distance_m,
                    sum(float(graph.edges[item].length_m)
                        for item in route[:edge_index]),
                )
                queue.append({
                    "task_id": task_id,
                    "vehicle_id": assignment.vehicle_id,
                    "task_priority": int(task_by_id[task_id].priority),
                    "estimated_arrival_s": distance_to_edge / speed_mps,
                    "edge_traversal_s": edge_length / speed_mps,
                    "speed_source": "configured_vehicle_target_speed",
                })

        queue.sort(key=lambda item: (
            item["estimated_arrival_s"], -item["task_priority"], item["vehicle_id"]
        ))
        available_at = min(item["estimated_arrival_s"] for item in queue) + float(
            parameters["initial_blockage_seconds"]
        )
        decisions = []
        for queue_index, item in enumerate(queue, start=1):
            entry = max(float(item["estimated_arrival_s"]), available_at)
            wait = entry - float(item["estimated_arrival_s"])
            exit_time = entry + float(item["edge_traversal_s"])
            decisions.append({
                **item,
                "queue_position": queue_index,
                "scheduled_entry_s": round(entry, 6),
                "scheduled_exit_s": round(exit_time, 6),
                "estimated_wait_s": round(wait, 6),
                "action_type": (
                    "hold_for_safe_headway" if wait > 1e-9
                    else "authorize_bottleneck_entry"
                ),
                "bottleneck_edge_id": bottleneck_edge_id,
                "minimum_safety_headway_seconds": parameters[
                    "minimum_safety_headway_seconds"
                ],
                "time_source": "route_length_and_configured_speed_surrogate",
                "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
                "constraint_results": {
                    "nonnegative_wait": wait >= -1e-9,
                    "entry_not_before_available_slot": entry + 1e-9 >= available_at,
                    "positive_traversal_window": exit_time > entry,
                    "vehicle_assignment_retained": True,
                },
                "policy_version": POLICY_VERSION,
            })
            available_at = exit_time + float(
                parameters["minimum_safety_headway_seconds"]
            )

        for task in tasks:
            task.status = "completed"
            task.completed_tick = parameters["recovery_tick"]
            task.status_reason = (
                "structural_mock_completion_after_traffic_control"
                if task.task_id in affected_task_ids
                else "structural_mock_completion_unaffected"
            )
        adapter.complete_tasks(tasks)
        waits = [float(item["estimated_wait_s"]) for item in decisions]
        return {
            "status": "PASS", "mode": "mock_structural",
            "simulation_claim": (
                "shared_road_queue_and_time_surrogate_only_no_carla_traffic"
            ),
            "scenario_id": dynamic.scenario_id, "seed": effective_seed,
            "scenario_source": "map_resources_topology_and_global_p5",
            "random_mode": "seeded_structural_congestion_mock_only",
            "congestion_event": {
                "event_type": "shared_road_capacity_degradation",
                "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                **parameters,
            },
            "bottleneck_edge_id": bottleneck_edge_id,
            "traffic_control_status_during_event": "ACTIVE",
            "traffic_control_status_after_recovery": "RELEASED",
            "route_impact_task_ids": affected_task_ids,
            "affected_task_count": len(affected_task_ids),
            "unaffected_task_count": len(tasks) - len(affected_task_ids),
            "traffic_decisions": decisions,
            "conflict_resolution_count": max(0, len(decisions) - 1),
            "estimated_total_wait_s": round(sum(waits), 6),
            "estimated_max_wait_s": round(max(waits), 6),
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
                "P5/topology facts plus parameterized capacity-one queue and "
                "time surrogates; no CARLA Traffic Manager, collision, physical "
                "driving or real-mine traffic validation."
            ),
        }
    finally:
        adapter.close()
