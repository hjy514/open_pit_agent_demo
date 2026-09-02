"""Offline structural runner for selective road-closure response."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..adapters.mock_adapter import MockAdapter
from ..map_resources import MapResourceStore
from ..map_resources.road_graph import identify_affected_routes, route_plans_from_store
from ..scheduler import BaselineScheduler, tasks_from_zones
from .episode import build_episode
from .fleet import snapshot_from_episode
from .road_state import RoadState


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
        initial = scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
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
        if run_store is not None:
            run_store.record_event(episode.run_id, stamp, "task_replanned", {"tick": 30, "task_id": target.task_id, "vehicle_id": replanned[0].vehicle_id, "reason": "road_closure"})
        for task in tasks:
            task.status = "completed"
            task.completed_tick = 31
            task.status_reason = "structural_mock_completion_after_road_replan" if task.task_id == target.task_id else "structural_mock_completion"
            if run_store is not None:
                run_store.record_event(episode.run_id, stamp, "task_completed", {"tick": 31, "task_id": task.task_id, "status": task.status, "reason": task.status_reason})
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
        }
    finally:
        adapter.close()
        if owned_map_store:
            map_resource_store.close()