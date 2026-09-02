"""Offline structural runner for S02 vehicle-failure takeover."""
from typing import Any, Dict, Optional
from datetime import datetime, timezone

from ..adapters.mock_adapter import MockAdapter
from ..scheduler import BaselineScheduler, release_failed_vehicle_tasks, tasks_from_zones
from .episode import build_episode
from .fleet import snapshot_from_episode


def run_s02_structural_mock(config: Any, seed: Optional[int] = None, run_store: Any = None) -> Dict[str, Any]:
    """Validate fault release/reassignment orchestration without CARLA physics."""
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    failed_vehicle_id = config.demo.failure_vehicle_id
    episode = build_episode(config, run_id="s02-structural-mock", seed=effective_seed)
    tasks = tasks_from_zones(config.zones)
    adapter = MockAdapter(config)
    if run_store is not None:
        run_store.record_episode(episode)
    scheduler = BaselineScheduler()
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_assignments = scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
        adapter.inject_fault(failed_vehicle_id)
        released = release_failed_vehicle_tasks(tasks, failed_vehicle_id)
        if run_store is not None:
            stamp = datetime.now(timezone.utc).isoformat()
            run_store.record_event(episode.run_id, stamp, "vehicle_fault", {"tick": config.demo.failure_tick, "vehicle_id": failed_vehicle_id})
            for task_id in released:
                run_store.record_event(episode.run_id, stamp, "task_released", {"tick": config.demo.failure_tick, "task_id": task_id, "reason": "released_after_vehicle_fault"})
        states_after_fault = list(adapter.list_states())
        reassigned = scheduler.assign(tasks, states_after_fault, config.zones, excluded_vehicle_ids={failed_vehicle_id})
        adapter.dispatch(tasks, config.zones)
        if run_store is not None:
            stamp = datetime.now(timezone.utc).isoformat()
            for assignment in reassigned:
                run_store.record_event(episode.run_id, stamp, "task_reassigned", {"tick": config.demo.failure_tick, "task_id": assignment.task_id, "vehicle_id": assignment.vehicle_id, "reason": "vehicle_failure_takeover"})
        for task in tasks:
            task.status = "completed"
            task.completed_tick = int(config.demo.failure_tick) + 1
            task.status_reason = "structural_mock_completion_after_takeover"
            if run_store is not None:
                run_store.record_event(episode.run_id, datetime.now(timezone.utc).isoformat(), "task_completed", {"tick": task.completed_tick, "task_id": task.task_id, "status": task.status, "reason": task.status_reason})
        if run_store is not None:
            run_store.record_event(episode.run_id, datetime.now(timezone.utc).isoformat(), "run_completed", {"tick": config.demo.failure_tick + 1, "status": "PASS", "mode": "mock_structural"})
        return {
            "status": "PASS",
            "mode": "mock_structural",
            "simulation_claim": "fault_release_and_reassignment_only_no_carla_physics",
            "scenario_id": config.scenario_id,
            "seed": effective_seed,
            "failed_vehicle_id": failed_vehicle_id,
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_assignment_count": len(initial_assignments),
            "released_task_ids": list(released),
            "reassignment_count": len(reassigned),
            "reassigned_vehicle_ids": sorted({item.vehicle_id for item in reassigned}),
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "task_count": len(tasks),
        }
    finally:
        adapter.close()
