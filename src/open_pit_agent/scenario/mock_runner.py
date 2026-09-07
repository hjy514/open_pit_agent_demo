"""Offline structural runner for S01; it does not simulate CARLA physics."""
from typing import Any, Dict, Optional, Sequence

from ..models import Task
from ..scheduler import BaselineScheduler, tasks_from_zones
from ..adapters import ExecutionCommand, ExecutionManager, MockAdapter
from ..closed_loop import ClosedLoopCoordinator
from ..runtime_state import RuntimeState
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot


def run_s01_structural_mock(config: Any, seed: Optional[int] = None,
                            tasks: Optional[Sequence[Task]] = None,
                            scheduler: Optional[BaselineScheduler] = None,
                            include_candidate_rankings: bool = False,
                            execution_context: Optional[Dict[str, Any]] = None,
                            closed_loop_coordination: bool = False,
                            ) -> Dict[str, Any]:
    """Run assignment and terminal-state checks without claiming movement validation."""
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    episode = build_episode(config, run_id="s01-structural-mock", seed=effective_seed)
    tasks = list(tasks) if tasks is not None else tasks_from_zones(config.zones)
    adapter = MockAdapter(config)
    scheduler = scheduler or BaselineScheduler()
    execution_context = dict(execution_context or {})
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        candidate_rankings = {
            task.task_id: [item.to_dict() for item in scheduler.rank_candidates(
                task, states, config.zones
            )]
            for task in tasks
        } if include_candidate_rankings else None
        manager = ExecutionManager(adapter, physical_execution=False)
        coordination_result = None
        if closed_loop_coordination:
            holder: Dict[str, Any] = {}

            def schedule(context):
                assignments = scheduler.assign(tasks, states, config.zones)
                holder["assignments"] = assignments
                holder["command"] = ExecutionCommand(
                    command_id="{}:dispatch".format(episode.run_id),
                    action_type="dispatch_tasks",
                    task_ids=tuple(task.task_id for task in tasks),
                    assignments={
                        task.task_id: str(task.assigned_vehicle_id)
                        for task in tasks
                    },
                    issued_by=str(execution_context.get(
                        "issued_by", "multi_objective_optimizer"
                    )),
                    safety_gate_status=str(execution_context.get(
                        "safety_gate_status", "REJECTED"
                    )),
                    metadata={"simulation_mode": "structural"},
                )
                return {
                    "status": "SCHEDULED",
                    "assignments": [item.to_dict() for item in assignments],
                    "command": holder["command"].to_dict(),
                }

            def execute(context):
                dispatch_feedback = manager.dispatch(
                    holder["command"], tasks, config.zones
                )
                if dispatch_feedback.status != "SUCCEEDED":
                    return [dispatch_feedback.to_dict()]
                holder["dispatched_vehicle_states"] = vehicle_state_snapshot(
                    adapter.list_states()
                )
                for task in tasks:
                    task.status = "completed"
                    task.completed_tick = 1
                    task.status_reason = "structural_mock_completion"
                adapter.complete_tasks(tasks)
                terminal_feedback = manager.observe(
                    holder["command"], tasks, phase="terminal",
                    status="SUCCEEDED",
                )
                return [dispatch_feedback.to_dict(), terminal_feedback.to_dict()]

            coordinator = ClosedLoopCoordinator(
                RuntimeState(),
                risk_stage=lambda context: {
                    "status": "NOT_APPLICABLE_NORMAL_BASELINE",
                    "risk_level": "BLUE",
                },
                decision_stage=lambda context: {
                    "status": "DECIDED",
                    "policy_version": execution_context.get("issued_by"),
                    "candidate_ranking_count": len(candidate_rankings or {}),
                },
                scheduling_stage=schedule,
                planning_stage=lambda context: {
                    "status": "P5_ROUTE_FACTS_ADMITTED",
                    "physical_route_execution": False,
                },
                safety_stage=lambda context: {
                    "status": execution_context.get(
                        "safety_gate_status", "REJECTED"
                    ),
                    "reviews": list(execution_context.get(
                        "safety_reviews", []
                    )),
                },
                execution_stage=execute,
            )
            coordination_result = coordinator.run_cycle({
                "run_id": episode.run_id,
                "vehicles": initial_vehicle_states,
                "tasks": [task.to_dict() for task in tasks],
                "roads": {}, "environment": {}, "monitoring": {},
                "risk": {}, "traffic": {}, "equipment": {},
            })
            if coordination_result["status"] != "SUCCEEDED":
                raise RuntimeError(
                    "coordinated structural execution failed: {}".format(
                        coordination_result["status"]
                    )
                )
            assignments = holder["assignments"]
            command = holder["command"]
            dispatched_vehicle_states = holder["dispatched_vehicle_states"]
            execution_feedback = coordination_result["execution_feedback"]
        else:
            assignments = scheduler.assign(tasks, states, config.zones)
            command = ExecutionCommand(
                command_id="{}:dispatch".format(episode.run_id),
                action_type="dispatch_tasks",
                task_ids=tuple(task.task_id for task in tasks),
                assignments={
                    task.task_id: str(task.assigned_vehicle_id) for task in tasks
                },
                issued_by=str(execution_context.get(
                    "issued_by", "structural_baseline_scheduler"
                )),
                safety_gate_status=str(execution_context.get(
                    "safety_gate_status", "NOT_AVAILABLE_LEGACY_BASELINE"
                )),
                metadata={"simulation_mode": "structural"},
            )
            dispatch_feedback = manager.dispatch(command, tasks, config.zones)
            if dispatch_feedback.status != "SUCCEEDED":
                raise RuntimeError(
                    dispatch_feedback.error or "structural dispatch failed"
                )
            dispatched_vehicle_states = vehicle_state_snapshot(
                adapter.list_states()
            )
            # Structural completion checks orchestration, not physical travel.
            for task in tasks:
                task.status = "completed"
                task.completed_tick = 1
                task.status_reason = "structural_mock_completion"
            adapter.complete_tasks(tasks)
            terminal_feedback = manager.observe(
                command, tasks, phase="terminal", status="SUCCEEDED"
            )
            execution_feedback = [
                dispatch_feedback.to_dict(), terminal_feedback.to_dict()
            ]
        result = {
            "status": "PASS",
            "mode": "mock_structural",
            "simulation_claim": "assignment_and_dataflow_only_no_carla_physics",
            "scenario_id": config.scenario_id,
            "seed": effective_seed,
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_vehicle_states": initial_vehicle_states,
            "dispatched_vehicle_states": dispatched_vehicle_states,
            "final_vehicle_states": vehicle_state_snapshot(adapter.list_states()),
            "assignment_count": len(assignments),
            "task_count": len(tasks),
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "assignments": [item.to_dict() for item in assignments],
            "tasks": [task.to_dict() for task in tasks],
            "execution_commands": [command.to_dict()],
            "execution_feedback": execution_feedback,
        }
        if coordination_result is not None:
            result["closed_loop_cycle"] = coordination_result
            result["closed_loop_coordination"] = "executed"
            result["execution_contract_mode"] = (
                "coordinated_structural_execution"
            )
        if candidate_rankings is not None:
            result["candidate_rankings"] = candidate_rankings
        return result
    finally:
        adapter.close()
