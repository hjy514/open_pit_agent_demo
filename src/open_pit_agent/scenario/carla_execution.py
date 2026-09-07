"""Unified CARLA execution bridge for admitted multi-scenario workloads."""
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..adapters.carla_adapter import CarlaAdapter
from ..adapters.base import ExecutionCommand, ExecutionManager
from ..closed_loop import ClosedLoopCoordinator
from ..map_resources import MapResourceStore
from ..runtime_state import RuntimeState
from .fleet import vehicle_state_snapshot
from .models import ScenarioLifecycle, normalize_scenario_run_result
from .random_s01 import prepare_random_map_workload


TERMINAL_TASK_STATES = {"completed", "timed_out", "cancelled"}
CARLA_SCENARIOS = ("s01", "s02", "s03", "s04", "s05", "s06", "s07", "s09")


def _assignment_map(items: Any) -> Dict[str, str]:
    output = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        vehicle_id = item.get("vehicle_id") or item.get("assigned_vehicle_id")
        if vehicle_id:
            output[str(item["task_id"])] = str(vehicle_id)
    return output


def _event_ticks(scenario: str, result: Dict[str, Any], ticks: int) -> Tuple[int, int]:
    event, recovery = 0, 0
    payload = {}
    if scenario == "s02":
        event = int(result.get("failure_tick") or 30)
    elif scenario == "s03":
        payload = result.get("equipment_event", {})
        event, recovery = int(payload.get("failure_tick") or 30), int(payload.get("recovery_tick") or 70)
    elif scenario == "s04":
        payload = result.get("blast_event", {})
        event, recovery = int(payload.get("blast_start_tick") or 40), int(payload.get("clearance_tick") or 60)
    elif scenario in {"s05", "s06"}:
        payload = result.get("weather_event", {}) if scenario == "s05" else result.get("congestion_event", {})
        event, recovery = int(payload.get("event_tick") or 30), int(payload.get("recovery_tick") or 60)
    elif scenario == "s07":
        event = int(result.get("closure_tick") or 30)
    elif scenario == "s09":
        ordered = result.get("compound_events", [])
        event = min([int(item.get("tick") or 30) for item in ordered] or [30])
        recovery = max([int(item.get("tick") or event) for item in ordered] or [event])
    if event:
        event = min(event, max(1, ticks // 3))
    if recovery:
        recovery = min(max(event + 1, recovery), max(event + 1, (ticks * 2) // 3))
    return event, recovery


def _emit(adapter: Any, event_type: str, payload: Dict[str, Any]) -> None:
    emitter = getattr(adapter, "_emit", None)
    if callable(emitter):
        emitter(event_type, payload)


def _apply_primary_event(scenario: str, structural: Dict[str, Any],
                         workload: Dict[str, Any], tasks: List[Any],
                         adapter: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Translate approved structural decisions into adapter operations."""
    applied, paused = [], []
    task_by_id = {item.task_id: item for item in tasks}
    vehicle_speed = {item.vehicle_id: float(item.target_speed_kmh)
                     for item in workload["vehicles"]}

    def record(action: str, payload: Dict[str, Any]) -> None:
        item = {"action": action, "status": "APPLIED", **payload}
        applied.append(item)
        _emit(adapter, "scenario_control_applied", item)

    if scenario == "s02":
        failed = structural.get("failed_vehicle_id")
        if failed:
            adapter.inject_fault(str(failed))
            record("inject_vehicle_fault", {"vehicle_id": str(failed)})
        final_map = _assignment_map(structural.get("tasks", []))
        for task_id in structural.get("released_task_ids", []):
            task = task_by_id.get(str(task_id))
            selected = final_map.get(str(task_id))
            if task is None or not selected or task.status in TERMINAL_TASK_STATES:
                continue
            task.original_vehicle_id = task.assigned_vehicle_id
            task.assigned_vehicle_id = None
            task.status = "released"
            record("reassign_released_task", dict(
                adapter.reassign_task(str(task_id), selected)
            ))

    if scenario == "s03":
        zones = {item.zone_id: item for item in workload["zones"]}
        for decision in structural.get("equipment_decisions", []):
            task_id = str(decision.get("task_id"))
            task, zone = task_by_id.get(task_id), zones.get(task_id)
            target = str(decision.get("alternative_work_point_id") or "")
            if task is None or zone is None or task.status in TERMINAL_TASK_STATES:
                continue
            try:
                spawn_index = int(target.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            record("retarget_after_equipment_failure", dict(
                adapter.retarget_task(
                    task_id, replace(zone, target_spawn_point_index=spawn_index)
                )
            ))

    if scenario == "s04":
        for decision in structural.get("blast_decisions", []):
            if decision.get("action_type") == "hold_until_blast_clearance":
                vehicle_id = str(decision.get("vehicle_id"))
                paused.append(vehicle_id)
                record("hold_for_blast_clearance", dict(adapter.pause_vehicle(vehicle_id)))
            elif decision.get("vehicle_id") and decision.get("task_id"):
                task = task_by_id.get(str(decision.get("task_id")))
                if task is not None and task.status not in TERMINAL_TASK_STATES:
                    record("blast_zone_safe_route", dict(adapter.reassign_task(
                        task.task_id, str(decision.get("vehicle_id"))
                    )))

    if scenario == "s05":
        factor = float(structural.get("weather_event", {}).get("restricted_speed_factor") or 1.0)
        for decision in structural.get("weather_decisions", []):
            task_id = str(decision.get("task_id"))
            task = task_by_id.get(task_id)
            if task is None or task.status in TERMINAL_TASK_STATES:
                continue
            speed = vehicle_speed.get(str(decision.get("vehicle_id")), 20.0) * factor
            record("apply_weather_speed_limit", dict(
                adapter.set_task_speed_limit(task_id, speed)
            ))

    if scenario == "s06":
        for decision in structural.get("traffic_decisions", [])[1:]:
            vehicle_id = str(decision.get("vehicle_id"))
            paused.append(vehicle_id)
            record("hold_for_safe_headway", dict(adapter.pause_vehicle(vehicle_id)))

    if scenario in {"s07", "s09"}:
        for decision in structural.get("route_changes", []):
            task_id = str(decision.get("task_id"))
            task, selected = task_by_id.get(task_id), decision.get("vehicle_id")
            if task is None or not selected or task.status in TERMINAL_TASK_STATES:
                continue
            record(str(decision.get("action_type") or "replan_task"), dict(
                adapter.reassign_task(task_id, str(selected))
            ))
    return applied, paused


def _apply_recovery(scenario: str, structural: Dict[str, Any],
                    workload: Dict[str, Any], tasks: List[Any], adapter: Any,
                    paused: List[str]) -> List[Dict[str, Any]]:
    applied = []
    # In S09 this slot is the ordered second event, not a recovery: only
    # after road-closure replanning has been applied may the vehicle fail.
    if scenario == "s09":
        failed = structural.get("failed_vehicle_id")
        if failed:
            adapter.inject_fault(str(failed))
            applied.append({
                "action": "inject_vehicle_fault_after_road_closure",
                "status": "APPLIED", "vehicle_id": str(failed),
            })
        final_map = _assignment_map(structural.get("tasks", []))
        task_by_id = {item.task_id: item for item in tasks}
        for task_id in structural.get("released_task_ids", []):
            task, selected = task_by_id.get(str(task_id)), final_map.get(str(task_id))
            if task is None or not selected or task.status in TERMINAL_TASK_STATES:
                continue
            task.original_vehicle_id = task.assigned_vehicle_id
            task.assigned_vehicle_id = None
            task.status = "released"
            response = adapter.reassign_task(str(task_id), selected)
            applied.append({
                "action": "compound_fault_task_takeover",
                "status": "APPLIED", **response,
            })
    for vehicle_id in paused:
        response = adapter.resume_vehicle(vehicle_id)
        applied.append({"action": "resume_after_event_clearance", "status": "APPLIED", **response})
    if scenario == "s05":
        speeds = {item.vehicle_id: item.target_speed_kmh for item in workload["vehicles"]}
        affected = {str(item.get("task_id")): str(item.get("vehicle_id"))
                    for item in structural.get("weather_decisions", [])}
        for task in tasks:
            if task.task_id in affected and task.status not in TERMINAL_TASK_STATES:
                response = adapter.set_task_speed_limit(
                    task.task_id, float(speeds.get(affected[task.task_id], 20.0))
                )
                applied.append({"action": "restore_weather_speed", "status": "APPLIED", **response})
    for item in applied:
        _emit(adapter, "scenario_control_recovered", item)
    return applied


def _physical_cycle(scenario: str, structural: Dict[str, Any],
                    workload: Dict[str, Any], command: Dict[str, Any],
                    feedback: List[Dict[str, Any]]) -> Dict[str, Any]:
    fixed = lambda payload: (lambda context: deepcopy(payload))
    initial_state = {
        "schema_version": "openpit.world-state.v1",
        "run_id": workload["config"].scenario_id,
        "vehicles": structural.get("initial_vehicle_states", []),
        "tasks": structural.get("initial_task_states", []),
        "roads": {"closed_edge_id": structural.get("closed_edge_id")},
        "environment": {"map_id": workload["config"].carla.map_name},
        "risk": {}, "traffic": structural.get("congestion_event", {}),
        "equipment": structural.get("equipment_event", {}),
    }
    coordinator = ClosedLoopCoordinator(
        RuntimeState(),
        risk_stage=fixed({"status": "EVENT_EVALUATED", "scenario_key": scenario}),
        decision_stage=fixed({"status": "DECIDED", "policy_version": structural.get("policy_version")}),
        scheduling_stage=fixed({"status": "SCHEDULED", "assignments": command["assignments"]}),
        planning_stage=fixed({"status": "PLANNED", "route_plans": structural.get("route_plans", [])}),
        safety_stage=fixed({"status": "APPROVED", "shield_mode": "execution_gate"}),
        execution_stage=lambda context: feedback,
    )
    return coordinator.run_cycle(initial_state)


def run_carla_scenario_execution(scenario: str, config: Any,
                                 seed: Optional[int] = None,
                                 vehicle_count: int = 6, ticks: int = 1000,
                                 load_map: bool = False,
                                 check_only: bool = False,
                                 execution_policy: str = "heuristic",
                                 physical_route_pairs: Optional[Any] = None,
                                 adapter_factory: Callable[..., Any] = CarlaAdapter,
                                 ) -> Dict[str, Any]:
    """Execute S01-S07/S09 through one CARLA and feedback contract."""
    name = str(scenario).lower()
    if name not in CARLA_SCENARIOS:
        raise ValueError("unsupported CARLA scenario: {}".format(scenario))
    if ticks < 1:
        raise ValueError("ticks must be at least 1")
    from .runner import run_structural_scenario

    lifecycle = ScenarioLifecycle()
    lifecycle.mark("prepare", "completed", {
        "scenario_key": name, "seed": seed, "vehicle_count": vehicle_count,
        "check_only": bool(check_only),
    })
    policy = execution_policy
    if policy == "auto":
        policy = "multi-objective" if name in {"s01", "s02"} else "heuristic"
    binding = config.map_resource
    if physical_route_pairs is None:
        with MapResourceStore(binding.database_path) as resource_store:
            physical_pairs = {
                (str(item["from_point_id"]), str(item["to_point_id"]))
                for item in resource_store.physical_route_validations(
                    binding.map_id, binding.resource_version
                )
                if item.get("validation_status") == "PHYSICAL_REACHED"
            }
    else:
        physical_pairs = {
            (str(item[0]), str(item[1])) for item in physical_route_pairs
        }
    if len(physical_pairs) < vehicle_count:
        raise ValueError(
            "CARLA随机场景只允许P6实跑成功路线；"
            "当前{}条，{}车场景至少需要{}条且起点独立。"
            "请先执行 ./map_resources.sh validate-physical "
            "--auto-candidates 12 --minimum-route-length-m 100 "
            "--maximum-route-length-m 500".format(
                len(physical_pairs), vehicle_count, vehicle_count
            )
        )
    structural = run_structural_scenario(
        name, config, seed=seed, random_map=True,
        vehicle_count=vehicle_count, execution_policy=policy,
        eligible_pairs=physical_pairs,
        minimum_length_m=100.0, maximum_length_m=1000.0,
    )
    workload_seed = structural.get("workload_seed", structural.get("seed", seed))
    workload = prepare_random_map_workload(
        config, seed=workload_seed, vehicle_count=vehicle_count,
        minimum_length_m=100.0, maximum_length_m=1000.0,
        scenario_key=name, eligible_pairs=physical_pairs,
    )
    configured_timeout_ticks = int(workload["config"].demo.task_timeout_ticks)
    effective_timeout_ticks = max(configured_timeout_ticks, int(ticks))
    if effective_timeout_ticks != configured_timeout_ticks:
        workload["config"] = replace(
            workload["config"],
            demo=replace(
                workload["config"].demo,
                task_timeout_ticks=effective_timeout_ticks,
            ),
        )
    initial_assignments = _assignment_map(
        structural.get("initial_assignments") or structural.get("assignments")
    )
    final_assignments = _assignment_map(structural.get("tasks", []))
    tasks = deepcopy(workload["tasks"])
    for task in tasks:
        selected = initial_assignments.get(task.task_id) or final_assignments.get(task.task_id)
        if not selected:
            raise RuntimeError("missing structural assignment for {}".format(task.task_id))
        task.assigned_vehicle_id = selected
        task.status = "assigned"
        task.status_reason = "structural_decision_approved_for_carla"
    assignment_map = {item.task_id: str(item.assigned_vehicle_id) for item in tasks}
    command = ExecutionCommand(
        command_id="{}:carla-dispatch".format(workload["config"].scenario_id),
        action_type="dispatch_tasks", task_ids=tuple(item.task_id for item in tasks),
        assignments=assignment_map,
        issued_by=str(structural.get("policy_version") or "structural_policy"),
        safety_gate_status="APPROVED",
        metadata={"simulation_mode": "carla", "seed": workload["seed"], "scenario_key": name},
    )
    adapter = adapter_factory(workload["config"], load_map=load_map)
    manager = ExecutionManager(adapter, physical_execution=True)
    events, feedback, controls = [], [], []
    ticks_executed, final_states, destroyed = 0, [], 0
    event_tick, recovery_tick = _event_ticks(name, structural, ticks)
    paused, event_applied, recovery_applied = [], False, False
    adapter.connect()
    lifecycle.mark("start", "ready" if check_only else "completed", {
        "execution_mode": "carla", "connected": True,
    })
    try:
        if check_only:
            result = {
                "status": "READY", "mode": "carla_execution_check",
                "scenario_key": name, "scenario_id": workload["config"].scenario_id,
                "seed": workload["seed"], "vehicle_count": vehicle_count,
                "task_count": len(tasks), "assignment_count": len(assignment_map),
                "event_tick": event_tick, "recovery_tick": recovery_tick,
                "configured_task_timeout_ticks": configured_timeout_ticks,
                "effective_task_timeout_ticks": effective_timeout_ticks,
                "spawn_point_indices": [item.spawn_point_index for item in workload["vehicles"]],
                "target_spawn_point_indices": [item.target_spawn_point_index for item in workload["zones"]],
                "simulation_claim": "carla_connection_and_workload_admission_only",
                "execution_commands": [command.to_dict()], "execution_feedback": [],
            }
            lifecycle.mark("finish", "READY", {})
            result["lifecycle"] = lifecycle.to_dict()
            return normalize_scenario_run_result(result, scenario_key=name)
        adapter.ensure_vehicles(spawn_missing=True)
        runtime_zones = adapter.resolve_zones(workload["zones"])
        dispatch_feedback = manager.dispatch(command, tasks, runtime_zones)
        feedback.append(dispatch_feedback.to_dict())
        if dispatch_feedback.status != "SUCCEEDED":
            raise RuntimeError(dispatch_feedback.error or "CARLA dispatch failed")
        events.extend(dispatch_feedback.events)
        for tick_index in range(ticks):
            if event_tick and tick_index == event_tick and not event_applied:
                current, paused = _apply_primary_event(name, structural, workload, tasks, adapter)
                controls.extend(current)
                event_applied = True
            if recovery_tick and tick_index == recovery_tick and not recovery_applied:
                controls.extend(_apply_recovery(name, structural, workload, tasks, adapter, paused))
                recovery_applied = True
            adapter.tick()
            ticks_executed = tick_index + 1
            events.extend(adapter.drain_events())
            if all(task.status in TERMINAL_TASK_STATES for task in tasks):
                break
        final_states = vehicle_state_snapshot(adapter.list_states())
        terminal_status = "SUCCEEDED" if all(item.status == "completed" for item in tasks) else "PARTIAL"
        feedback.append(manager.observe(
            command, tasks, phase="terminal", status=terminal_status, events=events
        ).to_dict())
    finally:
        if not check_only:
            destroyed = adapter.destroy_spawned_vehicles()
        adapter.close()

    completed = sum(item.status == "completed" for item in tasks)
    # Preserve the scenario's structured event/decision facts so database
    # validation can verify the same semantics against measured execution.
    result = dict(structural)
    result.update({
        "status": "PASS" if completed == len(tasks) else "PARTIAL",
        "mode": "carla_multi_scenario_execution", "scenario_key": name,
        "scenario_id": workload["config"].scenario_id, "seed": workload["seed"],
        "vehicle_count": vehicle_count, "task_count": len(tasks),
        "completed_task_count": completed,
        "terminal_task_count": sum(item.status in TERMINAL_TASK_STATES for item in tasks),
        "ticks_requested": ticks, "ticks_executed": ticks_executed,
        "all_tasks_completed": completed == len(tasks),
        "event_tick": event_tick, "recovery_tick": recovery_tick,
        "configured_task_timeout_ticks": configured_timeout_ticks,
        "effective_task_timeout_ticks": effective_timeout_ticks,
        "scenario_event_applied": event_applied, "runtime_controls": controls,
        "assignments": structural.get("assignments", []),
        "tasks": [item.to_dict() for item in tasks],
        "final_vehicle_states": final_states, "events": events,
        "event_count": len(events), "execution_commands": [command.to_dict()],
        "execution_feedback": feedback, "destroyed_vehicle_count": destroyed,
        "database_recording": False,
        "simulation_claim": "carla_basic_agent_multi_vehicle_event_execution",
        "closed_loop_validation": {
            "status": "CLOSED_LOOP_PASS" if completed == len(tasks) else "FAILED",
            "validation_mode": "carla_execution_feedback",
            "checks": [{
                "check": "all_physical_tasks_completed",
                "passed": completed == len(tasks),
                "detail": "{}/{}".format(completed, len(tasks)),
            }],
        },
        "boundary": (
            "CARLA actors and task controls are physically executed; scenario "
            "hazard/weather/closure facts are parameterized synthetic events. "
            "Topology avoidance remains the structural planner decision and is "
            "not a spawned physical road obstacle."
        ),
    })
    result["closed_loop_cycle"] = _physical_cycle(
        name, structural, workload, command.to_dict(), feedback
    )
    result["closed_loop_coordination"] = "executed_with_carla_feedback"
    lifecycle.mark("event", "completed" if event_applied else "not_applicable", {
        "runtime_control_count": len(controls),
    })
    lifecycle.mark("decision", "completed", {"assignment_count": len(assignment_map)})
    lifecycle.mark("execute", "completed" if result["status"] == "PASS" else "partial", {
        "ticks_executed": ticks_executed, "completed_task_count": completed,
    })
    lifecycle.mark("feedback", "completed", {"runtime_event_count": len(events)})
    lifecycle.mark("finish", result["status"], {"destroyed_vehicle_count": destroyed})
    result["lifecycle"] = lifecycle.to_dict()
    return normalize_scenario_run_result(result, scenario_key=name)


def run_s01_carla_execution(config: Any, seed: Optional[int] = None,
                            vehicle_count: int = 6, ticks: int = 1000,
                            load_map: bool = False, check_only: bool = False,
                            physical_route_pairs: Optional[Any] = None,
                            adapter_factory: Callable[..., Any] = CarlaAdapter,
                            ) -> Dict[str, Any]:
    """Backward-compatible S01 entry retained for callers and regression tests."""
    return run_carla_scenario_execution(
        "s01", config, seed=seed, vehicle_count=vehicle_count, ticks=ticks,
        load_map=load_map, check_only=check_only,
        physical_route_pairs=physical_route_pairs,
        adapter_factory=adapter_factory,
    )
