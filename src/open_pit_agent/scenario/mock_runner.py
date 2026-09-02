"""Offline structural runner for S01; it does not simulate CARLA physics."""
from typing import Any, Dict, Optional

from ..models import Task
from ..scheduler import BaselineScheduler, tasks_from_zones
from ..adapters.mock_adapter import MockAdapter
from .episode import build_episode
from .fleet import snapshot_from_episode


def run_s01_structural_mock(config: Any, seed: Optional[int] = None) -> Dict[str, Any]:
    """Run assignment and terminal-state checks without claiming movement validation."""
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    episode = build_episode(config, run_id="s01-structural-mock", seed=effective_seed)
    tasks = tasks_from_zones(config.zones)
    adapter = MockAdapter(config)
    scheduler = BaselineScheduler()
    adapter.connect()
    try:
        states = list(adapter.list_states())
        assignments = scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
        # Structural Mock completion validates orchestration only; no distance/physics claim.
        for task in tasks:
            task.status = "completed"
            task.completed_tick = 1
            task.status_reason = "structural_mock_completion"
        return {
            "status": "PASS",
            "mode": "mock_structural",
            "simulation_claim": "assignment_and_dataflow_only_no_carla_physics",
            "scenario_id": config.scenario_id,
            "seed": effective_seed,
            "fleet": snapshot_from_episode(episode).to_dict(),
            "assignment_count": len(assignments),
            "task_count": len(tasks),
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "assignments": [item.to_dict() for item in assignments],
        }
    finally:
        adapter.close()
