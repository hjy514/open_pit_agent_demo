"""Offline structural runner for S02 vehicle-failure takeover."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from random import Random
from typing import Any, Dict, Optional, Sequence, Set, Tuple
from datetime import datetime, timezone

from ..adapters import ExecutionCommand, ExecutionManager, MockAdapter
from ..closed_loop import ClosedLoopCoordinator
from ..decision import (CandidateCostInput, MapContext, MultiObjectiveCostModel,
                        OptimizationAssignmentAdapter, OptimizationScheduler,
                        load_cost_weights,
                        review_selected_candidate_rankings)
from ..models import Task, VehicleState
from ..runtime_state import RuntimeState
from ..scheduler import BaselineScheduler, release_failed_vehicle_tasks, tasks_from_zones
from .episode import build_episode
from .fleet import snapshot_from_episode, vehicle_state_snapshot
from .random_s01 import prepare_random_map_workload


def run_s02_structural_mock(config: Any, seed: Optional[int] = None,
                            run_store: Any = None,
                            tasks: Optional[Sequence[Task]] = None,
                            initial_scheduler: Optional[BaselineScheduler] = None,
                            takeover_scheduler: Optional[BaselineScheduler] = None,
                            include_candidate_rankings: bool = False,
                            execution_context: Optional[Dict[str, Any]] = None,
                            closed_loop_coordination: bool = False) -> Dict[str, Any]:
    """Validate fault release/reassignment orchestration without CARLA physics."""
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    failed_vehicle_id = config.demo.failure_vehicle_id
    episode = build_episode(config, run_id="s02-structural-mock", seed=effective_seed)
    tasks = list(tasks) if tasks is not None else tasks_from_zones(config.zones)
    adapter = MockAdapter(config)
    if run_store is not None:
        run_store.record_episode(episode)
    initial_scheduler = initial_scheduler or BaselineScheduler()
    takeover_scheduler = takeover_scheduler or BaselineScheduler()
    execution_context = dict(execution_context or {})
    adapter.connect()
    try:
        states = list(adapter.list_states())
        initial_vehicle_states = vehicle_state_snapshot(states)
        initial_assignments = initial_scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
        adapter.inject_fault(failed_vehicle_id)
        released = release_failed_vehicle_tasks(tasks, failed_vehicle_id)
        if run_store is not None:
            stamp = datetime.now(timezone.utc).isoformat()
            run_store.record_event(episode.run_id, stamp, "vehicle_fault", {"tick": config.demo.failure_tick, "vehicle_id": failed_vehicle_id})
            for task_id in released:
                run_store.record_event(episode.run_id, stamp, "task_released", {"tick": config.demo.failure_tick, "task_id": task_id, "reason": "released_after_vehicle_fault"})
        states_after_fault = list(adapter.list_states())
        fault_vehicle_states = vehicle_state_snapshot(states_after_fault)
        released_tasks = [task for task in tasks if task.task_id in set(released)]
        candidate_rankings = {
            task.task_id: [item.to_dict() for item in takeover_scheduler.rank_candidates(
                task, states_after_fault, config.zones, active_tasks=tasks,
                excluded_vehicle_ids={failed_vehicle_id},
            )]
            for task in released_tasks
        } if include_candidate_rankings else None
        coordination_result = None
        execution_feedback = []
        if closed_loop_coordination:
            holder: Dict[str, Any] = {}
            manager = ExecutionManager(adapter, physical_execution=False)

            def schedule(context):
                reassigned = takeover_scheduler.assign(
                    tasks, states_after_fault, config.zones,
                    excluded_vehicle_ids={failed_vehicle_id},
                )
                holder["reassigned"] = reassigned
                holder["command"] = ExecutionCommand(
                    command_id="{}:takeover".format(episode.run_id),
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
                    metadata={
                        "simulation_mode": "structural",
                        "event_type": "vehicle_failure_takeover",
                        "failed_vehicle_id": failed_vehicle_id,
                    },
                )
                if run_store is not None:
                    stamp = datetime.now(timezone.utc).isoformat()
                    for assignment in reassigned:
                        run_store.record_event(
                            episode.run_id, stamp, "task_reassigned",
                            {"tick": config.demo.failure_tick,
                             "task_id": assignment.task_id,
                             "vehicle_id": assignment.vehicle_id,
                             "reason": "vehicle_failure_takeover"},
                        )
                return {
                    "status": "TAKEOVER_SCHEDULED",
                    "failed_vehicle_id": failed_vehicle_id,
                    "released_task_ids": list(released),
                    "assignments": [item.to_dict() for item in reassigned],
                    "command": holder["command"].to_dict(),
                }

            def execute(context):
                dispatch_feedback = manager.dispatch(
                    holder["command"], tasks, config.zones
                )
                if dispatch_feedback.status != "SUCCEEDED":
                    return [dispatch_feedback.to_dict()]
                for task in tasks:
                    task.status = "completed"
                    task.completed_tick = int(config.demo.failure_tick) + 1
                    task.status_reason = (
                        "structural_mock_completion_after_takeover"
                    )
                    if run_store is not None:
                        run_store.record_event(
                            episode.run_id,
                            datetime.now(timezone.utc).isoformat(),
                            "task_completed",
                            {"tick": task.completed_tick,
                             "task_id": task.task_id,
                             "status": task.status,
                             "reason": task.status_reason},
                        )
                adapter.complete_tasks(tasks)
                terminal_feedback = manager.observe(
                    holder["command"], tasks, phase="terminal",
                    status="SUCCEEDED",
                )
                if run_store is not None:
                    run_store.record_event(
                        episode.run_id, datetime.now(timezone.utc).isoformat(),
                        "run_completed",
                        {"tick": config.demo.failure_tick + 1,
                         "status": "PASS", "mode": "mock_structural"},
                    )
                return [dispatch_feedback.to_dict(), terminal_feedback.to_dict()]

            coordinator = ClosedLoopCoordinator(
                RuntimeState(),
                risk_stage=lambda context: {
                    "status": "EVENT_CONFIRMED",
                    "event_type": "vehicle_failure",
                    "failed_vehicle_id": failed_vehicle_id,
                },
                decision_stage=lambda context: {
                    "status": "TAKEOVER_REQUIRED",
                    "released_task_ids": list(released),
                    "policy_version": execution_context.get("issued_by"),
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
                "vehicles": fault_vehicle_states,
                "tasks": [task.to_dict() for task in tasks],
                "roads": {}, "environment": {}, "monitoring": {},
                "risk": {}, "traffic": {}, "equipment": {},
            })
            if coordination_result["status"] != "SUCCEEDED":
                raise RuntimeError(
                    "coordinated takeover execution failed: {}".format(
                        coordination_result["status"]
                    )
                )
            reassigned = holder["reassigned"]
            execution_feedback = coordination_result["execution_feedback"]
        else:
            reassigned = takeover_scheduler.assign(
                tasks, states_after_fault, config.zones,
                excluded_vehicle_ids={failed_vehicle_id},
            )
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
            adapter.complete_tasks(tasks)
            if run_store is not None:
                run_store.record_event(episode.run_id, datetime.now(timezone.utc).isoformat(), "run_completed", {"tick": config.demo.failure_tick + 1, "status": "PASS", "mode": "mock_structural"})
        result = {
            "status": "PASS",
            "mode": "mock_structural",
            "simulation_claim": "fault_release_and_reassignment_only_no_carla_physics",
            "scenario_id": config.scenario_id,
            "seed": effective_seed,
            "failed_vehicle_id": failed_vehicle_id,
            "failure_tick": int(config.demo.failure_tick),
            "fleet": snapshot_from_episode(episode).to_dict(),
            "initial_vehicle_states": initial_vehicle_states,
            "fault_vehicle_states": fault_vehicle_states,
            "final_vehicle_states": vehicle_state_snapshot(adapter.list_states()),
            "initial_assignment_count": len(initial_assignments),
            "initial_assignments": [item.to_dict() for item in initial_assignments],
            "released_task_ids": list(released),
            "reassignment_count": len(reassigned),
            "reassigned_vehicle_ids": sorted({item.vehicle_id for item in reassigned}),
            "assignments": [item.to_dict() for item in reassigned],
            "completed_task_count": sum(task.status == "completed" for task in tasks),
            "task_count": len(tasks),
            "tasks": [task.to_dict() for task in tasks],
        }
        if coordination_result is not None:
            result["execution_commands"] = [
                holder["command"].to_dict()
            ]
            result["execution_feedback"] = execution_feedback
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


def run_random_s02_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   execution_policy: str = "heuristic",
                                   eligible_pairs: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Run one seeded map-backed failure/reassignment decision episode."""
    workload = prepare_random_map_workload(
        config, seed, vehicle_count, minimum_length_m, maximum_length_m, "s02",
        eligible_pairs=eligible_pairs,
    )
    effective_seed = workload["seed"]
    vehicles = workload["vehicles"]
    base_dynamic = workload["config"]
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

    initial_scheduler = BaselineScheduler(
        load_penalty=10000.0,
        distance_provider=route_distance,
        distance_label="p5_route_length",
        constrained_tasks_first=True,
        unique_vehicle_assignment=True,
    )
    takeover_scheduler = BaselineScheduler(
        load_penalty=10000.0,
        distance_provider=route_distance,
        distance_label="p5_route_length",
    )
    # Select only a failure whose assigned task has at least one legal P5
    # takeover candidate.  This is scenario admissibility, not a soft cost.
    probe_adapter = MockAdapter(base_dynamic)
    probe_tasks = deepcopy(workload["tasks"])
    probe_adapter.connect()
    try:
        probe_states = list(probe_adapter.list_states())
        probe_assignments = initial_scheduler.assign(
            probe_tasks, probe_states, base_dynamic.zones
        )
        task_by_id = {item.task_id: item for item in probe_tasks}
        recoverable_failure_ids = []
        for assignment in probe_assignments:
            task = task_by_id[assignment.task_id]
            alternatives = takeover_scheduler.rank_candidates(
                task,
                probe_states,
                base_dynamic.zones,
                active_tasks=probe_tasks,
                excluded_vehicle_ids={assignment.vehicle_id},
            )
            if alternatives:
                recoverable_failure_ids.append(assignment.vehicle_id)
    finally:
        probe_adapter.close()
    if not recoverable_failure_ids:
        raise ValueError(
            "seeded S02 workload has no recoverable vehicle-failure candidate"
        )
    failed_vehicle_id = Random(effective_seed + 2002).choice(
        sorted(set(recoverable_failure_ids))
    )
    dynamic = replace(
        base_dynamic,
        demo=replace(base_dynamic.demo, failure_vehicle_id=failed_vehicle_id),
    )
    task_templates = deepcopy(workload["tasks"])
    baseline_result = run_s02_structural_mock(
        dynamic,
        effective_seed,
        tasks=deepcopy(task_templates),
        initial_scheduler=initial_scheduler,
        takeover_scheduler=takeover_scheduler,
        include_candidate_rankings=True,
    )

    state_snapshots = {
        item["vehicle_id"]: item
        for item in baseline_result["fault_vehicle_states"]
    }
    task_objects = {item.task_id: item for item in task_templates}
    cost_config = Path(__file__).resolve().parents[3] / "configs" / "dispatch_cost_v1.json"
    cost_model = MultiObjectiveCostModel(load_cost_weights(cost_config))
    candidates_by_task = {}
    for task_id in baseline_result["released_task_ids"]:
        task = task_objects[task_id]
        candidates = []
        for vehicle_config in vehicles:
            snapshot = state_snapshots[vehicle_config.vehicle_id]
            route_length = route_costs.get(
                (vehicle_origins[vehicle_config.vehicle_id], zone_targets[task.zone_id])
            )
            vehicle_state = VehicleState(
                vehicle_id=vehicle_config.vehicle_id,
                display_name=vehicle_config.display_name,
                equipment_type=vehicle_config.equipment_type,
                role_name=vehicle_config.role_name,
                blueprint=vehicle_config.blueprint,
                capabilities=list(vehicle_config.capabilities),
                position=vehicle_config.mock_position,
                health=snapshot["health"],
                available=bool(snapshot["available"]),
                current_task_id=snapshot.get("current_task_id"),
                task_status=snapshot.get("task_status", "idle"),
            )
            candidates.append(CandidateCostInput(
                vehicle=vehicle_state,
                task=task,
                map_context=MapContext(
                    route_reachable=route_length is not None,
                    route_length_m=route_length,
                    planner_version="CARLA_GlobalRoutePlanner_0.9.10_res_2.000m",
                    validation_status=(
                        "PLANNER_REACHABLE" if route_length is not None else None
                    ),
                ),
                runtime_state={
                    "minimum_route_length_m": minimum_length_m,
                    "maximum_route_length_m": maximum_length_m,
                },
                metrics={
                    "nominal_speed_mps": vehicle_config.target_speed_kmh / 3.6,
                    "wait_time_s": None,
                    "task_delay_s": None,
                    "recovery_time_s": None,
                    "active_task_count": 1.0 if snapshot.get("current_task_id") else 0.0,
                },
                metric_status={
                    "wait_time_s": "not_available",
                    "task_delay_s": "not_available",
                    "recovery_time_s": "not_available",
                    "active_task_count": "available",
                },
            ))
        candidates_by_task[task_id] = candidates

    optimization = OptimizationScheduler(cost_model).optimize(candidates_by_task)
    v0_selected = {
        item["task_id"]: item["vehicle_id"]
        for item in baseline_result["assignments"]
    }
    baseline_safety_reviews = review_selected_candidate_rankings(
        optimization.candidate_rankings, v0_selected
    )
    if execution_policy == "heuristic":
        result = run_s02_structural_mock(
            dynamic,
            effective_seed,
            tasks=deepcopy(task_templates),
            initial_scheduler=initial_scheduler,
            takeover_scheduler=takeover_scheduler,
            include_candidate_rankings=True,
            execution_context={
                "issued_by": "heuristic-route-load-takeover-v0",
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
        result = run_s02_structural_mock(
            dynamic,
            effective_seed,
            tasks=deepcopy(task_templates),
            initial_scheduler=initial_scheduler,
            takeover_scheduler=OptimizationAssignmentAdapter(optimization),
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
        raise ValueError("unsupported S02 execution policy: {}".format(
            execution_policy
        ))
    v1_selected = {
        item["task_id"]: item["vehicle_id"] for item in optimization.assignments
    }
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
    v1_cost = fleet_metric(v1_selected, "total_cost")
    v0_route = fleet_metric(v0_selected, "cost_transport", "value")
    v1_route = fleet_metric(v1_selected, "cost_transport", "value")
    result.update({
        "scenario_source": "map_resources_global_p5",
        "map_resource_task_draft": workload["task_drafts"],
        "boundary": (
            "P5 planner facts plus structural fault/reassignment only; no CARLA "
            "physical driving, collision, clearance or multi-vehicle validation."
        ),
        "random_mode": "seeded_structural_failure_mock_only",
        "failure_selection": "seeded_redundant_role_vehicle",
        "policy_comparison": {
            "mode": (
                "multi_objective_v1_executed_with_v0_baseline"
                if execution_policy == "multi-objective"
                else "shadow_only_v1_not_executed"
            ),
            "decision_context": "vehicle_failure_takeover",
            "requested_policy": execution_policy,
            "initial_assignment_policy": "heuristic-route-load-global-unique-v0",
            "executed_policy": (
                "multi-objective-cost-v1"
                if execution_policy == "multi-objective"
                else "heuristic-route-load-takeover-v0"
            ),
            "comparison_policy": (
                "heuristic-route-load-takeover-v0"
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
                v0_selected.get(task_id) != v1_selected.get(task_id)
                for task_id in candidates_by_task
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
            "comparisons": [
                {
                    "task_id": task_id,
                    "executed_vehicle_v0": v0_selected.get(task_id),
                    "shadow_selected_vehicle_v1": v1_selected.get(task_id),
                    "selection_changed": (
                        v0_selected.get(task_id) != v1_selected.get(task_id)
                    ),
                    "v1_candidate_ranking": optimization.candidate_rankings[task_id],
                }
                for task_id in sorted(candidates_by_task)
            ],
        },
    })
    return result
