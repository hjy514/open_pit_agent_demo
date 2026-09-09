"""Unified CARLA execution bridge for admitted multi-scenario workloads."""
from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
import json
from math import ceil, cos, radians, sin, sqrt
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..adapters.carla_adapter import CarlaAdapter
from ..adapters.base import ExecutionCommand, ExecutionManager
from ..closed_loop import ClosedLoopCoordinator
from ..map_resources import MapResourceStore, route_plans_from_store
from ..runtime_state import RuntimeState
from .catalog import load_scenario_catalog, scenario_spec
from .fleet import vehicle_state_snapshot
from .models import ScenarioLifecycle, normalize_scenario_run_result
from .random_s01 import prepare_random_map_workload


TERMINAL_TASK_STATES = {"completed", "timed_out", "cancelled", "stuck"}
CARLA_SCENARIOS = ("s01", "s02", "s03", "s04", "s05", "s06", "s07", "s09")
P6_TICK_SECONDS_ESTIMATE = 0.05
P6_RUNTIME_MARGIN = 1.35
P6_RUNTIME_FIXED_MARGIN_TICKS = 200
# P5 proves planner reachability but has no measured travel time.  Its runtime
# watchdog budget therefore uses a deliberately conservative planning
# surrogate.  This value affects only how long CARLA may try; it is not an
# execution speed command or a measured-performance claim.
P5_BUDGET_SPEED_MPS = 3.0
# The map resource library currently proves individual routes, not concurrent
# fleet clearance. Stagger departures more as the requested fleet grows, then
# allow concurrent BasicAgent execution. This is an execution guard, not a
# claim that it proves multi-vehicle collision avoidance.
FLEET_IMMEDIATE_LAUNCH_COUNT = 1
FLEET_BASE_LAUNCH_GAP_TICKS = 120
FLEET_DENSITY_GAP_TICKS = 20
RUNTIME_TELEMETRY_INTERVAL_TICKS = 100
# Runtime traffic coordination is a shared safety service for every CARLA
# scenario, not an S06-only event.  It supplements (and does not replace)
# static route-edge admission: the latter prevents known conflicts before
# launch, while this guard reacts to measured actor positions when topology is
# incomplete or two routes converge at runtime.
TRAFFIC_COORDINATION_INTERVAL_TICKS = 20
TRAFFIC_CONFLICT_DISTANCE_M = 32.0
TRAFFIC_SAME_DIRECTION_DISTANCE_M = 20.0
TRAFFIC_CONFLICT_RELEASE_DISTANCE_M = 42.0
TRAFFIC_MAX_ELEVATION_DELTA_M = 4.0
TRAFFIC_FORWARD_CONE_COSINE = 0.20
TRAFFIC_HEADING_ALIGNMENT_COSINE = 0.75
TRAFFIC_LOW_SPEED_MPS = 0.80
TRAFFIC_BLOCK_CONFIRMATION_SAMPLES = 2
TRAFFIC_MIN_HOLD_TICKS = 40
TRAFFIC_ESCALATION_TICKS = 400
# These are lifecycle guards, not normal scene durations.  An event's seeded
# time is only its earliest eligible time; a live fleet must first launch and
# demonstrate measurable task progress.  A stalled task receives one
# navigation restart before being reported as a controlled failure.
EVENT_POST_LAUNCH_SETTLE_TICKS = 120
EVENT_MIN_PROGRESS_M = 10.0
STUCK_WINDOW_TICKS = 600
STUCK_MIN_PROGRESS_M = 5.0
STUCK_MIN_MOTION_SPEED_MPS = 0.2
MAX_NAVIGATION_RESTARTS = 1
MAX_FORWARD_RECOVERY_MANEUVERS = 1
MAX_RUNTIME_TASK_REASSIGNMENTS = 1
SAFETY_WATCHDOG_EXTENSION_TICKS = STUCK_WINDOW_TICKS
P6_DEFAULT_CONCURRENT_ROUTE_CAPACITY = 1
CARLA_LOADING_SERVICE_TICKS = 100
CARLA_DUMPING_SERVICE_TICKS = 60


def _assignment_map(items: Any) -> Dict[str, str]:
    output = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        vehicle_id = item.get("vehicle_id") or item.get("assigned_vehicle_id")
        if vehicle_id:
            output[str(item["task_id"])] = str(vehicle_id)
    return output


def _production_cycle_plans(
    workload: Dict[str, Any], physical_pairs: Any, planner_pairs: Any,
    assignments: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Build service plans only for haul-transport tasks.

    A dispatched task is one directed mission.  Loading and dumping belong to
    haul transport; inspection and support tasks complete at their destination.
    An empty return is a separate future task and is not inferred by reversing
    an outbound route that may have no directed route evidence.
    """
    plans = []
    physical = {(str(a), str(b)) for a, b in physical_pairs}
    planner = {(str(a), str(b)) for a, b in planner_pairs}
    task_types = {
        str(task.task_id): str(task.task_type)
        for task in workload.get("tasks", [])
    }
    for draft in workload.get("task_drafts", []):
        task_id = str(draft["task_id"])
        if task_types.get(task_id) != "haul_transport":
            continue
        target = str(draft["to_point_id"])
        assigned_vehicle_id = str(
            (assignments or {}).get(task_id)
            or draft["vehicle_id"]
        )
        assigned_origin = str(draft.get(
            "service_origin_point_id", draft["from_point_id"]
        ))
        reverse = (target, assigned_origin)
        outbound = (assigned_origin, target)
        outbound_evidence = (
            "P6_PHYSICAL_REACHED" if outbound in physical else
            "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION"
            if outbound in planner else "CARLA_RUNTIME_PLANNING_REQUIRED"
        )
        return_evidence = (
            "P6_PHYSICAL_REACHED" if reverse in physical else
            "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION"
            if reverse in planner else
            "CARLA_RUNTIME_PLANNING_REQUIRED"
        )
        vehicle = next(
            item for item in workload["vehicles"]
            if item.vehicle_id == assigned_vehicle_id
        )
        plans.append({
            "task_id": task_id,
            "vehicle_id": assigned_vehicle_id,
            "origin_spawn_point_index": int(vehicle.spawn_point_index),
            "origin_point_id": assigned_origin,
            "vehicle_spawn_point_id": workload.get(
                "vehicle_spawn_points", {}
            ).get(assigned_vehicle_id, assigned_origin),
            "dump_point_id": target,
            "loading_ticks": CARLA_LOADING_SERVICE_TICKS,
            "dumping_ticks": CARLA_DUMPING_SERVICE_TICKS,
            "outbound_route_evidence": outbound_evidence,
            "return_route_evidence": return_evidence,
            "completion_after_dumping": True,
            "task_completion_semantics": "destination_service_completed",
            "return_execution": "SEPARATE_TASK_NOT_EXECUTED",
        })
    return plans


def _task_mission_plans(
    workload: Dict[str, Any], physical_pairs: Any, planner_pairs: Any,
    assignments: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Build the shared spawn -> service origin -> service target contract."""
    physical = {(str(a), str(b)) for a, b in physical_pairs}
    planner = {(str(a), str(b)) for a, b in planner_pairs}
    vehicles = {
        str(item.vehicle_id): item for item in workload.get("vehicles", [])
    }
    zones = {
        str(item.zone_id): item for item in workload.get("zones", [])
    }
    task_types = {
        str(item.task_id): str(item.task_type)
        for item in workload.get("tasks", [])
    }
    plans = []
    for draft in workload.get("task_drafts", []):
        task_id = str(draft["task_id"])
        vehicle_id = str(assignments.get(task_id) or draft["vehicle_id"])
        vehicle = vehicles[vehicle_id]
        zone = zones[task_id]
        spawn_point_id = str(workload.get(
            "vehicle_spawn_points", {}
        ).get(vehicle_id) or "carla-spawn:{}".format(
            int(vehicle.spawn_point_index)
        ))
        service_origin_id = str(draft.get(
            "service_origin_point_id", draft["from_point_id"]
        ))
        service_target_id = str(draft.get(
            "service_target_point_id", draft["to_point_id"]
        ))
        deadhead_pair = (spawn_point_id, service_origin_id)
        deadhead_evidence = (
            "NOT_REQUIRED_ALREADY_AT_SERVICE_ORIGIN"
            if deadhead_pair[0] == deadhead_pair[1]
            else "P6_PHYSICAL_REACHED" if deadhead_pair in physical
            else "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION"
            if deadhead_pair in planner
            else "CARLA_RUNTIME_PLANNING_REQUIRED"
        )
        mission_pair = (service_origin_id, service_target_id)
        mission_evidence = (
            "P6_PHYSICAL_REACHED" if mission_pair in physical
            else "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION"
            if mission_pair in planner else "CARLA_RUNTIME_PLANNING_REQUIRED"
        )
        plans.append({
            "schema_version": "openpit.task-mission-plan.v1",
            "task_id": task_id,
            "task_type": task_types.get(task_id),
            "vehicle_id": vehicle_id,
            "vehicle_spawn_point_id": spawn_point_id,
            "vehicle_spawn_point_index": int(vehicle.spawn_point_index),
            "service_origin_point_id": service_origin_id,
            "service_origin_spawn_point_index": int(
                service_origin_id.rsplit(":", 1)[-1]
            ),
            "service_target_point_id": service_target_id,
            "service_target_spawn_point_index": int(
                zone.target_spawn_point_index
            ),
            "requires_deadhead": spawn_point_id != service_origin_id,
            "deadhead_route_length_m": draft.get("deadhead_route_length_m"),
            "deadhead_route_evidence": deadhead_evidence,
            "mission_route_length_m": draft.get("route_length_m"),
            "mission_route_evidence": mission_evidence,
            "completion_semantics": "service_target_arrival_or_service_completed",
        })
    return plans


def _validate_physical_route_admission(
    mission_plans: List[Dict[str, Any]], physical_pairs: Any,
) -> Dict[str, Any]:
    """Fail closed unless every initial CARLA leg has P6 success evidence.

    P5 remains useful for topology search and future calibration candidates,
    but it is not sufficient evidence for admitting a route to the shared
    multi-scenario CARLA executor. A vehicle already located at its service
    origin does not need a deadhead validation record.
    """
    physical = {(str(a), str(b)) for a, b in physical_pairs}
    rejected = []
    admitted = []
    for plan in mission_plans:
        task_id = str(plan.get("task_id"))
        deadhead = (
            str(plan.get("vehicle_spawn_point_id")),
            str(plan.get("service_origin_point_id")),
        )
        mission = (
            str(plan.get("service_origin_point_id")),
            str(plan.get("service_target_point_id")),
        )
        failures = []
        if deadhead[0] != deadhead[1] and deadhead not in physical:
            failures.append({
                "leg": "deadhead", "from_point_id": deadhead[0],
                "to_point_id": deadhead[1],
                "reason": "P6_PHYSICAL_REACHED_REQUIRED",
            })
        if mission not in physical:
            failures.append({
                "leg": "mission", "from_point_id": mission[0],
                "to_point_id": mission[1],
                "reason": "P6_PHYSICAL_REACHED_REQUIRED",
            })
        record = {
            "task_id": task_id,
            "vehicle_id": plan.get("vehicle_id"),
            "deadhead_pair": list(deadhead),
            "mission_pair": list(mission),
        }
        if failures:
            record["failures"] = failures
            rejected.append(record)
        else:
            admitted.append(record)
    result = {
        "status": "PASS" if not rejected else "REJECTED",
        "policy": "P6_PHYSICAL_REACHED_REQUIRED",
        "task_count": len(mission_plans),
        "admitted_task_count": len(admitted),
        "rejected_task_count": len(rejected),
        "admitted_tasks": admitted,
        "rejected_tasks": rejected,
        "boundary": (
            "Initial single-truck route evidence only; this does not prove "
            "multi-vehicle collision, clearance or traffic safety."
        ),
    }
    if rejected:
        details = "; ".join(
            "{}:{}".format(
                item["task_id"],
                ",".join(
                    "{} {}->{}".format(
                        failure["leg"], failure["from_point_id"],
                        failure["to_point_id"],
                    ) for failure in item["failures"]
                ),
            ) for item in rejected
        )
        raise ValueError(
            "P6_ROUTE_ADMISSION_FAILED: {}. P5只能作为候选路线，"
            "不允许降级进入CARLA执行".format(details)
        )
    return result


def _carla_production_runtime(
    tasks: List[Any], events: List[Dict[str, Any]],
    production_configuration: Dict[str, Any],
) -> Dict[str, Any]:
    transitions = []
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != (
                "task_production_stage_changed"):
            continue
        payload = dict(event.get("payload") or {})
        payload.setdefault("tick", event.get("tick"))
        payload.setdefault("event_type", event["event_type"])
        transitions.append(payload)
    supported = production_configuration.get("status") == "CONFIGURED"
    completed = sum(item.status == "completed" for item in tasks)
    return {
        "schema_version": "openpit.production-runtime.v1",
        "status": (
            "PASS" if supported and completed == len(tasks)
            else "PARTIAL" if supported else "NOT_SUPPORTED_BY_ADAPTER"
        ),
        "mode": "CARLA_MEASURED_PRODUCTION_EXECUTION" if supported else (
            "LEGACY_POINT_TO_POINT_ADAPTER"
        ),
        "task_count": len(tasks),
        "haul_service_task_count": int(
            production_configuration.get("task_count") or 0
        ),
        "completed_task_count": completed,
        "transitions": transitions,
        "service_model": production_configuration.get("service_model"),
        "boundary": (
            "Haul tasks use CARLA travel plus stationary loading/dumping "
            "service holds and complete at the directed destination. "
            "Inspection/support tasks use point-to-point execution. Material "
            "flow and production quantities are not physically simulated."
            if supported else
            "Adapter did not expose the common production-cycle interface."
        ),
    }


def _task_execution_diagnostics(tasks: List[Any],
                                controls: Optional[List[Dict[str, Any]]] = None
                                ) -> List[Dict[str, Any]]:
    """Classify measured execution outcomes without inventing root causes.

    CARLA BasicAgent exposes route progress but this project does not yet
    attach collision, lidar or traffic-manager sensors.  A stopped vehicle is
    therefore evidence of *no progress*, not evidence that another truck,
    terrain or an obstacle caused it.  The explicit category is useful for
    closed-loop datasets and tells later work exactly which sensor evidence is
    still missing.
    """
    controls_by_task = {}
    for control in controls or []:
        task_id = control.get("task_id") if isinstance(control, dict) else None
        if task_id:
            controls_by_task.setdefault(str(task_id), []).append(control)

    diagnostics = []
    for item in tasks:
        task_controls = controls_by_task.get(str(item.task_id), [])
        restarted = sum(
            1 for control in task_controls
            if control.get("action") == "restart_stalled_navigation"
        )
        reason = str(item.status_reason or "")
        if item.status == "completed":
            category, follow_up = "COMPLETED", False
        elif item.status == "timed_out":
            category = (
                "CAPACITY_HOLD_TIMEOUT"
                if reason == "route_capacity_hold_at_safety_watchdog_limit"
                else "TASK_TIMEOUT"
            )
            follow_up = True
        elif item.status == "cancelled":
            category, follow_up = "CANCELLED", True
        elif item.status == "stuck":
            if reason == "scenario_safety_watchdog_tick_limit":
                category = "SAFETY_WATCHDOG_LIMIT"
            elif restarted:
                category = "NAVIGATION_NO_PROGRESS_AFTER_RESTART"
            else:
                category = "NO_MEASURABLE_ROUTE_PROGRESS"
            follow_up = True
        else:
            category, follow_up = "NOT_TERMINAL", True
        diagnostics.append({
            "task_id": item.task_id,
            "assigned_vehicle_id": item.assigned_vehicle_id,
            "status": item.status,
            "status_reason": item.status_reason,
            "attempt_count": item.attempt_count,
            "last_distance_m": item.last_distance_m,
            "started_tick": item.started_tick,
            "completed_tick": item.completed_tick,
            "failure_category": category,
            "navigation_restart_count": restarted,
            "requires_follow_up": follow_up,
            "root_cause": (
                None if category == "COMPLETED" else "NOT_IDENTIFIED"
            ),
            "evidence_boundary": (
                "Route-distance, task-state and watchdog evidence only; "
                "no collision, obstacle, terrain or traffic-cause claim."
            ),
        })
    return diagnostics


def _execution_failure_summary(diagnostics: List[Dict[str, Any]]) -> Dict[str, int]:
    """Small, schema-stable aggregate for evidence and later dataset export."""
    summary = {}
    for item in diagnostics:
        category = str(item.get("failure_category") or "UNKNOWN")
        summary[category] = summary.get(category, 0) + 1
    return dict(sorted(summary.items()))


def _physical_speed_caps(workload: Dict[str, Any],
                         physical_validations: List[Dict[str, Any]]
                         ) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Return P6-evidence-backed speed caps for the selected task routes.

    A route proven with one truck at 15 km/h is not proof that the same route
    is stable at an arbitrary scenario-configured speed.  This is deliberately
    a conservative *execution cap*, not a change to the scheduler's target
    speed or a claim of multi-vehicle validation.
    """
    validated_speeds = {}
    validated_tolerances = {}
    for item in physical_validations:
        if item.get("validation_status") != "PHYSICAL_REACHED":
            continue
        speed = item.get("target_speed_kmh")
        if speed is None or float(speed) <= 0:
            continue
        pair = (str(item.get("from_point_id")), str(item.get("to_point_id")))
        validated_speeds[pair] = min(
            float(speed), validated_speeds.get(pair, float(speed))
        )
        tolerance = item.get("arrival_tolerance_m")
        if tolerance is not None and float(tolerance) > 0:
            validated_tolerances[pair] = min(
                float(tolerance),
                validated_tolerances.get(pair, float(tolerance)),
            )

    caps, evidence = {}, []
    for item in workload.get("task_drafts", []):
        pair = (str(item.get("from_point_id")), str(item.get("to_point_id")))
        speed = validated_speeds.get(pair)
        if speed is None:
            continue
        task_id = str(item["task_id"])
        caps[task_id] = speed
        evidence.append({
            "task_id": task_id,
            "from_point_id": pair[0], "to_point_id": pair[1],
            "speed_limit_kmh": speed,
            "arrival_tolerance_m": validated_tolerances.get(pair),
            "source": "P6_ISOLATED_SINGLE_TRUCK_VALIDATION",
        })
    return caps, evidence


def _fleet_runtime_sample(adapter: Any, tasks: List[Any], tick_index: int) -> Dict[str, Any]:
    """Capture measured CARLA state without assigning a cause to a stall."""
    by_vehicle = {}
    for state in adapter.list_states():
        position = getattr(state, "position", None)
        by_vehicle[str(state.vehicle_id)] = {
            "vehicle_id": str(state.vehicle_id),
            "current_task_id": getattr(state, "current_task_id", None),
            "task_status": getattr(state, "task_status", None),
            "speed_mps": round(float(getattr(state, "speed_mps", 0.0)), 4),
            "position": None if position is None else {
                "x": round(float(position.x), 3),
                "y": round(float(position.y), 3),
                "z": round(float(position.z), 3),
            },
        }
    return {
        "tick": int(tick_index),
        "tasks": [{
            "task_id": task.task_id,
            "assigned_vehicle_id": task.assigned_vehicle_id,
            "status": task.status,
            "last_distance_m": task.last_distance_m,
            "vehicle": by_vehicle.get(str(task.assigned_vehicle_id)),
        } for task in tasks],
    }


def _publish_runtime_snapshot(runtime_publisher: Optional[Callable[[Dict[str, Any]], None]],
                              adapter: Any, tasks: List[Any], workload: Dict[str, Any],
                              structural: Dict[str, Any], scenario: str,
                              tick_index: int, phase: str,
                              event: Optional[Dict[str, Any]] = None,
                              outcome: Optional[Dict[str, Any]] = None,
                              decision_override: Optional[Dict[str, Any]] = None) -> None:
    """Publish a factual CARLA snapshot when a caller supplies a sink.

    The execution bridge deliberately has no HTTP dependency.  The command-line
    entry owns transport to the Runtime API, while this function owns the
    simulator facts: current actor state, task state, scenario context and the
    event currently being applied.  A broken UI/API must never stop CARLA.
    """
    if runtime_publisher is None:
        return
    try:
        states = list(adapter.list_states())
        map_environment = getattr(adapter, "get_map_environment", None)
        environment = (
            dict(map_environment()) if callable(map_environment) else {}
        )
        environment.update({
            "map_name": workload["config"].carla.map_name,
            "simulation_mode": "carla_multi_scenario_execution",
            "physical_execution": True,
        })
        def state_dict(item):
            if hasattr(item, "to_dict"):
                return item.to_dict()
            position = getattr(item, "position", None)
            target = getattr(item, "target_position", None)
            return {
                "vehicle_id": str(getattr(item, "vehicle_id", "unknown")),
                "display_name": str(getattr(item, "vehicle_id", "unknown")),
                "equipment_type": getattr(item, "role_name", "multi_role_truck"),
                "status": getattr(item, "task_status", "unknown"),
                "task_status": getattr(item, "task_status", "unknown"),
                "health": getattr(item, "health", "unknown"),
                "available": bool(getattr(item, "available", False)),
                "current_task_id": getattr(item, "current_task_id", None),
                "position": None if position is None else {
                    "x": float(position.x), "y": float(position.y),
                    "z": float(position.z),
                },
                "target_position": None if target is None else {
                    "x": float(target.x), "y": float(target.y),
                    "z": float(target.z),
                },
            }

        snapshot = {
            "run_id": workload["config"].scenario_id,
            "map_name": workload["config"].carla.map_name,
            "world_state": {
                "schema_version": "openpit.world-state.v1",
                "run_id": workload["config"].scenario_id,
                "vehicles": [state_dict(item) for item in states],
                "tasks": [item.to_dict() for item in tasks],
                "roads": dict(structural.get("world_state", {}).get("roads") or {}),
                "environment": environment,
                "risk": dict(structural.get("world_state", {}).get("risk") or {}),
                "traffic": dict(structural.get("world_state", {}).get("traffic") or {}),
                "equipment": dict(structural.get("world_state", {}).get("equipment") or {}),
                "monitoring": {
                    "phase": phase,
                    "phase_index": int(tick_index),
                    "risk_level": "SCENARIO_EVENT" if event else "NORMAL_OPERATION",
                    "latest_event": (event or {}).get("message"),
                    "simulation_claim": "CARLA_BASIC_AGENT_MULTI_VEHICLE_EXECUTION",
                },
            },
            "run_context": {
                "run_id": workload["config"].scenario_id,
                "scenario_key": scenario,
                "scenario_id": workload["config"].scenario_id,
                "seed": workload["seed"],
            },
            "execution": {
                "mode": "carla_multi_scenario_execution",
                "physical_execution": True,
                "phase": phase,
                "status": "RUNNING" if outcome is None else outcome.get("status"),
                "tick": int(tick_index),
            },
            "outcome": dict(outcome or {"status": "RUNNING"}),
            "decision": decision_override or {
                "status": "EVENT_RESPONSE_PENDING" if event else "EXECUTING",
                "policy_version": structural.get("policy_version"),
                "decision_points": list(structural.get("decision_points", [])),
            },
            "assignments": list(structural.get("assignments", [])),
        }
        if event:
            snapshot["event"] = dict(event)
        runtime_publisher(snapshot)
    except Exception:
        # The publisher is an observability side channel.  It may fail because
        # the API/UI is stopped, without changing physical execution semantics.
        return


def _event_review_points(structural: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Select the pre-built, review-required response decisions for an event."""
    return [
        dict(item) for item in structural.get("decision_points", [])
        if isinstance(item, dict)
        and item.get("decision_point_id")
        and item.get("review_policy") == "REQUIRED_BEFORE_EXECUTION"
    ]


def _load_task_route_edges(workload: Dict[str, Any]) -> Dict[str, List[str]]:
    """Load static topology membership for selected task routes.

    Missing topology remains missing.  It is never inferred from Euclidean
    distance and never promoted to physical multi-truck validation.
    """
    config = workload.get("config")
    binding = getattr(config, "map_resource", None)
    if binding is None or binding.database_path is None:
        return {}
    endpoint_pairs = {
        str(item["task_id"]): (
            str(item.get("service_origin_point_id") or item["from_point_id"]),
            str(item.get("service_target_point_id") or item["to_point_id"]),
        )
        for item in workload.get("task_drafts", [])
        if item.get("task_id") and item.get("from_point_id") and item.get("to_point_id")
    }
    deadhead_pairs = {
        "{}#deadhead".format(item["task_id"]): (
            str(item["spawn_point_id"]),
            str(item.get("service_origin_point_id") or item["from_point_id"]),
        )
        for item in workload.get("task_drafts", [])
        if item.get("task_id") and item.get("spawn_point_id")
        and item.get("requires_deadhead")
    }
    with MapResourceStore(binding.database_path) as store:
        mission_plans = route_plans_from_store(
            store, binding.map_id, binding.resource_version, endpoint_pairs
        )
        deadhead_plans = route_plans_from_store(
            store, binding.map_id, binding.resource_version, deadhead_pairs
        )
    combined = {}
    for task_id in endpoint_pairs:
        edges = list(deadhead_plans.get(
            "{}#deadhead".format(task_id), []
        )) + list(mission_plans.get(task_id, []))
        if edges:
            combined[task_id] = list(dict.fromkeys(edges))
    return combined


def _runtime_route_waypoints(config: Any, structural: Dict[str, Any]
                             ) -> Dict[Tuple[str, ...], List[Dict[str, float]]]:
    """Resolve selected static road-edge sequences into CARLA coordinates."""
    decisions = []
    for key in (
        "equipment_decisions", "weather_decisions", "blast_decisions",
        "route_changes",
    ):
        decisions.extend(
            item for item in structural.get(key, []) if isinstance(item, dict)
        )
    takeover = structural.get("takeover_decision")
    if isinstance(takeover, dict):
        decisions.append(takeover)
    sequences = set()
    for item in decisions:
        edge_ids = (
            item.get("selected_edge_ids")
            or item.get("replanned_edge_ids")
            or item.get("route_edge_ids")
            or []
        )
        if edge_ids:
            sequences.add(tuple(str(edge_id) for edge_id in edge_ids))
    if not sequences:
        return {}
    binding = config.map_resource
    resolved = {}
    with MapResourceStore(binding.database_path) as store:
        for sequence in sequences:
            points = []
            complete = True
            for edge_id in sequence:
                row = store.connection.execute(
                    "SELECT geometry_json FROM road_edges WHERE edge_id=? "
                    "AND map_id=? AND resource_version=?",
                    (edge_id, binding.map_id, binding.resource_version),
                ).fetchone()
                if row is None or not row[0]:
                    complete = False
                    break
                try:
                    geometry = json.loads(row[0])
                except (TypeError, ValueError):
                    complete = False
                    break
                for raw in geometry:
                    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                        continue
                    point = {
                        "x": float(raw[0]), "y": float(raw[1]),
                        "z": float(raw[2]) if len(raw) > 2 else 0.0,
                    }
                    if not points or any(
                        abs(point[axis] - points[-1][axis]) > 0.01
                        for axis in ("x", "y", "z")
                    ):
                        points.append(point)
            if complete and points:
                resolved[sequence] = points
    return resolved


def _apply_selected_route(adapter: Any, task_id: str, vehicle_id: str,
                          decision: Dict[str, Any],
                          route_waypoints: Dict[Tuple[str, ...],
                                                List[Dict[str, float]]]
                          ) -> Optional[Dict[str, Any]]:
    """Forward a selected RoadGraph route when the adapter supports it."""
    method = getattr(adapter, "set_task_route", None)
    if not callable(method):
        return None
    edge_ids = (
        decision.get("selected_edge_ids")
        or decision.get("replanned_edge_ids")
        or decision.get("route_edge_ids")
        or []
    )
    signature = tuple(str(edge_id) for edge_id in edge_ids)
    points = route_waypoints.get(signature)
    if not signature or not points:
        return None
    return method(
        task_id, vehicle_id, points,
        blocked_edge_id=(
            decision.get("closed_edge_id")
            or decision.get("degraded_edge_id")
        ),
        route_edge_ids=list(signature),
    )


def _fleet_launch_schedule(
    workload: Dict[str, Any],
    route_edge_plans: Optional[Dict[str, List[str]]] = None,
    physical_route_capacity: int = P6_DEFAULT_CONCURRENT_ROUTE_CAPACITY,
) -> List[Dict[str, Any]]:
    """Create a deterministic, fleet-size-aware initial staging plan.

    Every requested vehicle is represented exactly once.  The first truck can
    depart immediately; later trucks wait a seed-independent headway.  The
    headway grows mildly after four vehicles so a 7--8 vehicle demonstration
    does not start as aggressively as a three-vehicle regression run.
    """
    drafts = list(workload.get("task_drafts", []))
    fleet_size = len({str(item.get("vehicle_id")) for item in drafts
                      if item.get("vehicle_id")})
    launch_gap = FLEET_BASE_LAUNCH_GAP_TICKS + max(0, fleet_size - 4) * (
        FLEET_DENSITY_GAP_TICKS
    )
    route_edge_plans = route_edge_plans or {}
    schedule = []
    for index, item in enumerate(drafts):
        task_id = str(item["task_id"])
        edge_ids = set(route_edge_plans.get(task_id, []))
        conflicts = []
        for prior in drafts[:index]:
            prior_task_id = str(prior["task_id"])
            prior_edges = set(route_edge_plans.get(prior_task_id, []))
            if (
                edge_ids and prior_edges
                and edge_ids.intersection(prior_edges)
            ):
                conflicts.append(prior_task_id)
        launch_tick = max(0, index - FLEET_IMMEDIATE_LAUNCH_COUNT + 1) * launch_gap
        schedule.append({
            "vehicle_id": str(item["vehicle_id"]),
            "task_id": task_id,
            "launch_tick": launch_tick,
            "mode": "immediate" if launch_tick == 0 else "headway_hold",
            "fleet_size": fleet_size,
            "launch_gap_ticks": launch_gap,
            "basis": "fleet_size_aware_initial_staging",
            "route_edge_count": len(edge_ids),
            "topology_conflicts_with_task_ids": conflicts,
            "physical_concurrent_route_capacity": None,
            "runtime_admission_strategy": (
                "TOPOLOGY_CONFLICT_SERIALIZED_WITH_HEADWAY"
            ),
            "release_condition": (
                "configured_headway_and_prior_route_conflicts_terminal"
                if conflicts else
                "configured_headway_elapsed"
                if launch_tick else "initial_route_admission"
            ),
        })
    return schedule


def _reconcile_route_admission(
    tasks: List[Any], adapter: Any, launch_schedule: List[Dict[str, Any]],
    state: Dict[str, Any], scenario_paused_vehicle_ids: Optional[List[str]] = None,
    tick_index: int = 0,
) -> List[Dict[str, Any]]:
    """Apply headway and serialize tasks that share known road edges.

    P6 remains isolated-route evidence and is not promoted to a multi-truck
    safety claim.  Tasks with proven disjoint edge sequences may execute in
    parallel; a later task sharing a known edge waits for its predecessors to
    become terminal.  Missing topology retains the conservative initial
    headway without pretending that an unknown route is conflict-free.
    """
    task_by_id = {str(item.task_id): item for item in tasks}
    schedule_by_task = {
        str(item["task_id"]): item for item in launch_schedule
    }
    scenario_paused = {str(item) for item in scenario_paused_vehicle_ids or []}
    eligibility_by_vehicle: Dict[str, List[bool]] = {}
    active_task_ids = []
    for task_id, task in task_by_id.items():
        if task.status in TERMINAL_TASK_STATES or not task.assigned_vehicle_id:
            continue
        launch_tick = int(schedule_by_task.get(task_id, {}).get("launch_tick", 0))
        conflicts = schedule_by_task.get(task_id, {}).get(
            "topology_conflicts_with_task_ids", []
        )
        unresolved_conflicts = [
            conflict_id for conflict_id in conflicts
            if conflict_id in task_by_id
            and task_by_id[conflict_id].status not in TERMINAL_TASK_STATES
        ]
        eligible = not (
            int(tick_index) < launch_tick or unresolved_conflicts
        )
        vehicle_id = str(task.assigned_vehicle_id)
        eligibility_by_vehicle.setdefault(vehicle_id, []).append(eligible)
        if eligible:
            active_task_ids.append(task_id)
    # A vehicle may own a queue after preemption/reassignment.  Do not pause
    # its currently executable task merely because a later queued task still
    # conflicts with another route.
    desired_held = {
        vehicle_id for vehicle_id, eligibility in eligibility_by_vehicle.items()
        if eligibility and not any(eligibility)
    }
    held = set(state.get("held_vehicle_ids") or [])
    controls = []
    for vehicle_id in sorted(desired_held - held):
        response = adapter.pause_vehicle(vehicle_id)
        controls.append({
            "action": "hold_initial_headway",
            "status": "APPLIED", "vehicle_id": vehicle_id,
            "tick": int(tick_index),
            **dict(response),
        })
    for vehicle_id in sorted(held - desired_held - scenario_paused):
        response = adapter.resume_vehicle(vehicle_id)
        controls.append({
            "action": "release_initial_headway",
            "status": "APPLIED", "vehicle_id": vehicle_id,
            "tick": int(tick_index),
            **dict(response),
        })
    state.update({
        "active_task_id": active_task_ids[0] if active_task_ids else None,
        "active_task_ids": sorted(active_task_ids),
        "active_vehicle_id": None,
        "held_vehicle_ids": sorted(desired_held),
        "admission_strategy": "TOPOLOGY_CONFLICT_SERIALIZED_WITH_HEADWAY",
    })
    return controls


def _state_position(state: Any) -> Optional[Tuple[float, float, float]]:
    position = getattr(state, "position", None)
    if position is None:
        return None
    try:
        return (
            float(position.x), float(position.y), float(position.z),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _traffic_pair_geometry(first: Any, second: Any) -> Optional[Dict[str, float]]:
    """Return factual pair geometry without claiming a collision cause."""
    first_position = _state_position(first)
    second_position = _state_position(second)
    if first_position is None or second_position is None:
        return None
    dx = second_position[0] - first_position[0]
    dy = second_position[1] - first_position[1]
    dz = second_position[2] - first_position[2]
    horizontal_distance = sqrt(dx * dx + dy * dy)
    if horizontal_distance < 0.001:
        horizontal_distance = 0.001
    first_yaw = radians(float(getattr(first, "yaw_deg", 0.0) or 0.0))
    second_yaw = radians(float(getattr(second, "yaw_deg", 0.0) or 0.0))
    first_forward_x, first_forward_y = cos(first_yaw), sin(first_yaw)
    second_forward_x, second_forward_y = cos(second_yaw), sin(second_yaw)
    return {
        "distance_m": horizontal_distance,
        "elevation_delta_m": abs(dz),
        "heading_alignment": (
            first_forward_x * second_forward_x
            + first_forward_y * second_forward_y
        ),
        "first_sees_second": (
            first_forward_x * dx + first_forward_y * dy
        ) / horizontal_distance,
        "second_sees_first": (
            second_forward_x * -dx + second_forward_y * -dy
        ) / horizontal_distance,
    }


def _traffic_task_by_vehicle(tasks: List[Any], states: List[Any]
                             ) -> Dict[str, Any]:
    by_task = {str(item.task_id): item for item in tasks}
    by_vehicle = {}
    for state in states:
        vehicle_id = str(getattr(state, "vehicle_id", "") or "")
        task_id = getattr(state, "current_task_id", None)
        task = by_task.get(str(task_id)) if task_id else None
        if task is not None:
            by_vehicle[vehicle_id] = task
    for task in tasks:
        vehicle_id = str(task.assigned_vehicle_id or "")
        if vehicle_id and vehicle_id not in by_vehicle:
            by_vehicle[vehicle_id] = task
    return by_vehicle


def _traffic_right_of_way(
    first: Any, second: Any, geometry: Dict[str, float],
    tasks_by_vehicle: Dict[str, Any], relation: str,
) -> Tuple[str, str, str]:
    """Choose one deterministic right-of-way owner and one yielding truck."""
    first_id = str(first.vehicle_id)
    second_id = str(second.vehicle_id)
    if relation == "same_direction_following":
        # Road order is safer than task priority for a following pair: the
        # front vehicle clears the lane and the rear vehicle yields.
        if geometry["first_sees_second"] >= geometry["second_sees_first"]:
            return second_id, first_id, "front_vehicle_clears_shared_lane"
        return first_id, second_id, "front_vehicle_clears_shared_lane"

    first_task = tasks_by_vehicle.get(first_id)
    second_task = tasks_by_vehicle.get(second_id)
    first_priority = int(getattr(first_task, "priority", 0) or 0)
    second_priority = int(getattr(second_task, "priority", 0) or 0)
    if first_priority != second_priority:
        if first_priority > second_priority:
            return first_id, second_id, "higher_task_priority"
        return second_id, first_id, "higher_task_priority"
    first_distance = getattr(first_task, "last_distance_m", None)
    second_distance = getattr(second_task, "last_distance_m", None)
    if first_distance is not None and second_distance is not None:
        if abs(float(first_distance) - float(second_distance)) > 0.5:
            if float(first_distance) < float(second_distance):
                return first_id, second_id, "shorter_remaining_task_distance"
            return second_id, first_id, "shorter_remaining_task_distance"
    winner = min(first_id, second_id)
    loser = second_id if winner == first_id else first_id
    return winner, loser, "deterministic_vehicle_id_tiebreak"


def _runtime_traffic_summary(state: Dict[str, Any], supported: bool,
                             check_only: bool = False) -> Dict[str, Any]:
    holds = state.get("holds", {})
    return {
        "schema_version": "openpit.runtime-traffic-coordination.v1",
        "status": (
            "READY_CHECK_ONLY" if check_only
            else "APPLIED" if supported else "NOT_SUPPORTED_BY_ADAPTER"
        ),
        "coordination_scope": "ALL_COMMON_CARLA_SCENARIOS",
        "detection_interval_ticks": TRAFFIC_COORDINATION_INTERVAL_TICKS,
        "conflict_distance_m": TRAFFIC_CONFLICT_DISTANCE_M,
        "release_distance_m": TRAFFIC_CONFLICT_RELEASE_DISTANCE_M,
        "detected_conflict_count": int(
            state.get("detected_conflict_count", 0)
        ),
        "resolved_conflict_count": int(
            state.get("resolved_conflict_count", 0)
        ),
        "escalated_conflict_count": int(
            state.get("escalated_conflict_count", 0)
        ),
        "active_holds": [dict(item) for item in holds.values()],
        "decisions": list(state.get("decisions", [])),
        "boundary": (
            "Measured position, heading, speed and task priority provide "
            "runtime right-of-way holds and releases. This is not a physical "
            "collision sensor, reverse manoeuvre controller, Traffic Manager "
            "or multi-vehicle safety certification. Persistent unresolved "
            "conflicts are escalated to the dispatch evidence stream."
        ),
    }


def _reconcile_runtime_traffic(
    tasks: List[Any], vehicle_states: List[Any], adapter: Any,
    state: Dict[str, Any], tick_index: int,
    protected_vehicle_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Coordinate measured multi-truck encounters for every common scenario.

    Known route conflicts are already serialized by ``_reconcile_route_admission``.
    This runtime layer covers converging actors and close following pairs using
    factual CARLA state.  It never invents a collision and never overrides a
    hold owned by the operator, an incident response or route admission.
    """
    protected = {str(item) for item in protected_vehicle_ids or []}
    holds = state.setdefault("holds", {})
    observations = state.setdefault("observations", {})
    decisions = state.setdefault("decisions", [])
    state.setdefault("detected_conflict_count", 0)
    state.setdefault("resolved_conflict_count", 0)
    state.setdefault("escalated_conflict_count", 0)
    by_vehicle = {
        str(item.vehicle_id): item for item in vehicle_states
        if getattr(item, "vehicle_id", None)
    }
    tasks_by_vehicle = _traffic_task_by_vehicle(tasks, vehicle_states)
    task_terminal_by_vehicle = {
        vehicle_id: task.status in TERMINAL_TASK_STATES
        for vehicle_id, task in tasks_by_vehicle.items()
    }
    controls = []

    # Release a yielding vehicle only after the right-of-way owner has cleared
    # the measured conflict area (or left the episode).  Another subsystem's
    # hold remains authoritative and is never resumed here.
    for yielding_id, hold in list(holds.items()):
        right_of_way_id = str(hold["right_of_way_vehicle_id"])
        yielding_state = by_vehicle.get(yielding_id)
        right_of_way_state = by_vehicle.get(right_of_way_id)
        geometry = (
            _traffic_pair_geometry(yielding_state, right_of_way_state)
            if yielding_state is not None and right_of_way_state is not None
            else None
        )
        hold_age = int(tick_index) - int(hold["hold_tick"])
        cleared = (
            yielding_state is None
            or task_terminal_by_vehicle.get(yielding_id, False)
            or right_of_way_state is None
            or task_terminal_by_vehicle.get(right_of_way_id, False)
            or (
                hold_age >= TRAFFIC_MIN_HOLD_TICKS
                and geometry is not None
                and geometry["distance_m"] >= TRAFFIC_CONFLICT_RELEASE_DISTANCE_M
            )
        )
        if cleared:
            response = {}
            action_status = "RELEASED_TO_OTHER_GUARD"
            if (
                yielding_state is not None
                and not task_terminal_by_vehicle.get(yielding_id, False)
                and yielding_id not in protected
            ):
                response = dict(adapter.resume_vehicle(yielding_id))
                action_status = "APPLIED"
            control = {
                "action": "resume_after_runtime_traffic_clearance",
                "status": action_status,
                "tick": int(tick_index),
                "vehicle_id": yielding_id,
                "right_of_way_vehicle_id": right_of_way_id,
                "conflict_id": hold["conflict_id"],
                "hold_duration_ticks": hold_age,
                **response,
            }
            controls.append(control)
            decisions.append(dict(control))
            state["resolved_conflict_count"] += 1
            _emit(adapter, "runtime_traffic_conflict_resolved", control)
            holds.pop(yielding_id, None)
            continue
        if (
            hold_age >= TRAFFIC_ESCALATION_TICKS
            and not hold.get("escalated")
        ):
            hold["escalated"] = True
            escalation = {
                "action": "escalate_persistent_runtime_traffic_conflict",
                "status": "DISPATCH_REVIEW_REQUIRED",
                "tick": int(tick_index),
                "vehicle_id": yielding_id,
                "right_of_way_vehicle_id": right_of_way_id,
                "conflict_id": hold["conflict_id"],
                "hold_duration_ticks": hold_age,
                "recommended_action": "replan_or_safe_hold",
            }
            controls.append(escalation)
            decisions.append(dict(escalation))
            state["escalated_conflict_count"] += 1
            _emit(adapter, "runtime_traffic_conflict_escalated", escalation)
        # An event recovery may have resumed a traffic-held actor.  Reassert
        # only this still-active traffic hold; do not touch protected actors.
        if (
            yielding_state is not None
            and yielding_id not in protected
            and str(getattr(yielding_state, "task_status", "")) != "paused"
        ):
            response = dict(adapter.pause_vehicle(yielding_id))
            control = {
                "action": "maintain_runtime_traffic_hold",
                "status": "APPLIED", "tick": int(tick_index),
                "vehicle_id": yielding_id,
                "conflict_id": hold["conflict_id"],
                **response,
            }
            controls.append(control)

    active_statuses = {
        "assigned", "executing", "deadhead_to_service_origin",
        "service_execution", "loaded_haul", "returning",
    }
    candidates = []
    for vehicle_id, vehicle_state in sorted(by_vehicle.items()):
        task = tasks_by_vehicle.get(vehicle_id)
        status = str(getattr(vehicle_state, "task_status", "") or "")
        if (
            task is None or task.status in TERMINAL_TASK_STATES
            or vehicle_id in protected or vehicle_id in holds
            or not bool(getattr(vehicle_state, "available", True))
            or str(getattr(vehicle_state, "health", "healthy")) != "healthy"
            or status not in active_statuses
            or _state_position(vehicle_state) is None
        ):
            continue
        candidates.append(vehicle_state)

    seen_pairs = set()
    for first_index, first in enumerate(candidates):
        for second in candidates[first_index + 1:]:
            pair = tuple(sorted((str(first.vehicle_id), str(second.vehicle_id))))
            pair_id = "{}::{}".format(pair[0], pair[1])
            geometry = _traffic_pair_geometry(first, second)
            if (
                geometry is None
                or geometry["distance_m"] > TRAFFIC_CONFLICT_DISTANCE_M
                or geometry["elevation_delta_m"] > TRAFFIC_MAX_ELEVATION_DELTA_M
            ):
                continue
            seen_pairs.add(pair_id)
            previous = observations.get(pair_id, {})
            consecutive = (
                int(previous.get("consecutive_samples", 0)) + 1
                if int(tick_index) - int(previous.get("last_tick", tick_index))
                <= TRAFFIC_COORDINATION_INTERVAL_TICKS * 2
                else 1
            )
            observations[pair_id] = {
                "last_tick": int(tick_index),
                "consecutive_samples": consecutive,
            }
            same_direction = (
                geometry["heading_alignment"]
                >= TRAFFIC_HEADING_ALIGNMENT_COSINE
                and geometry["distance_m"]
                <= TRAFFIC_SAME_DIRECTION_DISTANCE_M
                and max(
                    geometry["first_sees_second"],
                    geometry["second_sees_first"],
                ) >= TRAFFIC_FORWARD_CONE_COSINE
            )
            converging = (
                geometry["first_sees_second"] >= TRAFFIC_FORWARD_CONE_COSINE
                and geometry["second_sees_first"] >= TRAFFIC_FORWARD_CONE_COSINE
            )
            both_slow = (
                float(getattr(first, "speed_mps", 0.0) or 0.0)
                <= TRAFFIC_LOW_SPEED_MPS
                and float(getattr(second, "speed_mps", 0.0) or 0.0)
                <= TRAFFIC_LOW_SPEED_MPS
            )
            confirmed_blockage = (
                both_slow and converging
                and consecutive >= TRAFFIC_BLOCK_CONFIRMATION_SAMPLES
            )
            if not same_direction and not converging and not confirmed_blockage:
                continue
            relation = (
                "same_direction_following" if same_direction
                else "persistent_low_speed_blockage" if confirmed_blockage
                else "converging_route_conflict"
            )
            winner, yielding, reason = _traffic_right_of_way(
                first, second, geometry, tasks_by_vehicle, relation
            )
            if yielding in holds or yielding in protected or winner in holds:
                continue
            conflict_id = "traffic-{}-{}".format(pair_id, int(tick_index))
            response = dict(adapter.pause_vehicle(yielding))
            hold = {
                "conflict_id": conflict_id,
                "pair_vehicle_ids": list(pair),
                "yielding_vehicle_id": yielding,
                "right_of_way_vehicle_id": winner,
                "hold_tick": int(tick_index),
                "relation": relation,
                "decision_reason": reason,
                "detected_distance_m": round(geometry["distance_m"], 3),
                "escalated": False,
            }
            holds[yielding] = hold
            control = {
                "action": "hold_for_runtime_traffic_right_of_way",
                "status": "APPLIED", "tick": int(tick_index),
                "vehicle_id": yielding,
                "right_of_way_vehicle_id": winner,
                "conflict_id": conflict_id,
                "relation": relation,
                "decision_reason": reason,
                "distance_m": round(geometry["distance_m"], 3),
                **response,
            }
            controls.append(control)
            decisions.append(dict(control))
            state["detected_conflict_count"] += 1
            _emit(adapter, "runtime_traffic_right_of_way_granted", control)

    for pair_id, observation in list(observations.items()):
        if (
            pair_id not in seen_pairs
            and int(tick_index) - int(observation.get("last_tick", tick_index))
            > TRAFFIC_COORDINATION_INTERVAL_TICKS * 3
        ):
            observations.pop(pair_id, None)
    return controls


def _recommended_execution_ticks(workload: Dict[str, Any], scenario: str,
                                 physical_validations: List[Dict[str, Any]],
                                 launch_schedule: Optional[List[Dict[str, Any]]] = None,
                                 assignments: Optional[Dict[str, str]] = None,
                                 physical_route_capacity: int = P6_DEFAULT_CONCURRENT_ROUTE_CAPACITY,
                                 ) -> Tuple[int, Dict[str, Any]]:
    """Derive a conservative tick budget from P6 time or P5 route length.

    P6 evidence is isolated single-truck execution, so this is only a runtime
    budget—not a multi-vehicle success claim. P5 fallback uses route length
    and a declared conservative speed surrogate, never invented measurements.
    """
    durations = {}
    for item in physical_validations:
        if item.get("validation_status") != "PHYSICAL_REACHED":
            continue
        duration = item.get("duration_seconds")
        if duration is None or float(duration) <= 0:
            continue
        pair = (str(item["from_point_id"]), str(item["to_point_id"]))
        durations[pair] = max(float(duration), durations.get(pair, 0.0))
    selected_pairs = [
        (str(item["from_point_id"]), str(item["to_point_id"]))
        for item in workload.get("task_drafts", [])
    ]
    fallback = workload.get("generation", {}).get("metadata", {}).get("fallback_route", {})
    if scenario == "s02" and fallback:
        selected_pairs.append((str(fallback.get("from_point_id")),
                               str(fallback.get("to_point_id"))))
    selected_duration_seconds = []
    p6_matched_count = 0
    p5_estimated_count = 0
    for item in workload.get("task_drafts", []):
        pair = (str(item["from_point_id"]), str(item["to_point_id"]))
        duration = durations.get(pair)
        source = "P6_PHYSICAL_REACHED"
        if duration is None:
            route_length = item.get("route_length_m")
            if route_length is not None and float(route_length) > 0:
                duration = float(route_length) / P5_BUDGET_SPEED_MPS
                source = "P5_ROUTE_LENGTH_SURROGATE"
        if duration is None:
            continue
        selected_duration_seconds.append((
            str(item.get("task_id") or "route-{}".format(
                len(selected_duration_seconds)
            )),
            pair,
            float(duration),
            source,
        ))
        if source == "P6_PHYSICAL_REACHED":
            p6_matched_count += 1
        else:
            p5_estimated_count += 1

    fallback_duration = None
    if scenario == "s02" and fallback:
        fallback_pair = (
            str(fallback.get("from_point_id")),
            str(fallback.get("to_point_id")),
        )
        fallback_duration = durations.get(fallback_pair)
        if fallback_duration is None and fallback.get("route_length_m"):
            fallback_duration = (
                float(fallback["route_length_m"]) / P5_BUDGET_SPEED_MPS
            )
            p5_estimated_count += 1
        elif fallback_duration is not None:
            p6_matched_count += 1
    if not selected_duration_seconds and fallback_duration is None:
        return 0, {
            "status": "NOT_AVAILABLE",
            "reason": "no_route_duration_or_length_for_selected_routes",
            "selected_route_count": len(selected_pairs),
            "matched_route_count": 0, "estimated_route_count": 0,
        }
    # A vehicle can receive more than one task after a failure/reassignment.
    # A max(single route) budget cuts that queue short even though each route
    # is individually admitted, so calculate a conservative serial queue
    # duration per assigned vehicle.  Unknown P6 duration stays explicit and
    # never becomes fabricated timing evidence.
    task_duration_seconds = {
        task_id: duration
        for task_id, _pair, duration, _source in selected_duration_seconds
    }
    queue_duration_seconds = {}
    unmapped_task_count = 0
    for index, item in enumerate(workload.get("task_drafts", [])):
        task_id = str(item.get("task_id") or "route-{}".format(index))
        vehicle_id = (assignments or {}).get(task_id) or item.get("vehicle_id")
        duration = task_duration_seconds.get(task_id)
        if not vehicle_id or duration is None:
            unmapped_task_count += 1
            continue
        key = str(vehicle_id)
        queue_duration_seconds[key] = (
            queue_duration_seconds.get(key, 0.0) + float(duration)
        )
    physical_route_capacity = max(1, int(physical_route_capacity))
    all_route_durations = [
        item[2] for item in selected_duration_seconds
    ] + ([float(fallback_duration)] if fallback_duration is not None else [])
    critical_seconds = (
        sum(all_route_durations)
        if physical_route_capacity == 1
        else max(queue_duration_seconds.values() or all_route_durations)
    )
    launch_delay = max([int(item["launch_tick"]) for item in launch_schedule or []] or [0])
    recommended = int(ceil(
        critical_seconds / P6_TICK_SECONDS_ESTIMATE * P6_RUNTIME_MARGIN
    )) + P6_RUNTIME_FIXED_MARGIN_TICKS + launch_delay
    return recommended, {
        "status": (
            "P6_FLEET_DURATION_SURROGATE"
            if p5_estimated_count == 0
            else "P5_ROUTE_LENGTH_FLEET_DURATION_SURROGATE"
            if p6_matched_count == 0
            else "P6_P5_MIXED_FLEET_DURATION_SURROGATE"
        ),
        "critical_serial_queue_duration_seconds": round(critical_seconds, 3),
        "critical_route_duration_seconds": max(all_route_durations),
        "matched_route_count": p6_matched_count,
        "estimated_route_count": p5_estimated_count,
        "selected_route_count": len(selected_pairs),
        "p5_budget_speed_mps": (
            P5_BUDGET_SPEED_MPS if p5_estimated_count else None
        ),
        "queue_duration_seconds_by_vehicle": {
            key: round(value, 3) for key, value in sorted(queue_duration_seconds.items())
        },
        "unmapped_or_unmeasured_task_count": unmapped_task_count,
        "runtime_budget_concurrent_route_capacity": physical_route_capacity,
        "physical_concurrent_route_capacity": None,
        "capacity_source": "RUNTIME_BUDGET_ASSUMPTION_NOT_SAFETY_CLAIM",
        "tick_seconds_estimate": P6_TICK_SECONDS_ESTIMATE,
        "runtime_margin": P6_RUNTIME_MARGIN,
        "fixed_margin_ticks": P6_RUNTIME_FIXED_MARGIN_TICKS,
        "launch_delay_ticks": launch_delay,
    }


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
    return event, recovery


def _event_is_ready(scenario: str, scheduled_tick: int, tick_index: int,
                    launch_schedule: List[Dict[str, Any]], tasks: List[Any],
                    progress: Dict[str, Dict[str, Any]],
                    safety_watchdog_tick_limit: int) -> bool:
    """Require real fleet progress before a seeded incident can occur.

    The seeded tick supplies variation, but does not allow a fault or hazard
    popup before the affected work is actually under way.  Tiny fake-adapter
    tests do not expose distances, so they are allowed through after the same
    lifecycle gate; production CARLA states always provide distances.
    """
    last_launch = max([
        int(item.get("launch_tick", 0)) for item in launch_schedule
    ] or [0])
    readiness_tick = max(int(scheduled_tick or 0), last_launch + EVENT_POST_LAUNCH_SETTLE_TICKS)
    # Keep small deterministic adapter tests practical while real episodes
    # still use the full post-launch gate.
    readiness_tick = min(readiness_tick, max(1, safety_watchdog_tick_limit // 4))
    if tick_index < readiness_tick:
        return False
    failed_vehicle = None
    if scenario in {"s02", "s09"}:
        # The progress map stores this hint when the structural result is
        # attached by the caller below.
        failed_vehicle = progress.get("__failed_vehicle__", {}).get("vehicle_id")
    relevant = [
        task for task in tasks
        if task.status not in TERMINAL_TASK_STATES
        and (failed_vehicle is None or task.assigned_vehicle_id == failed_vehicle)
    ]
    samples = [progress.get(task.task_id, {}) for task in relevant]
    observed = [item for item in samples if item.get("initial_distance_m") is not None]
    if not observed:
        return True
    return any(float(item.get("progress_m") or 0.0) >= EVENT_MIN_PROGRESS_M
               for item in observed)


def _update_progress_watchdog(tasks: List[Any], tick_index: int,
                              adapter: Any,
                              progress: Dict[str, Dict[str, Any]],
                              task_speed_caps: Dict[str, float],
                              held_vehicle_ids: Optional[List[str]] = None,
                              vehicle_speed_mps: Optional[Dict[str, float]] = None,
                              recovery_attempts: Optional[Dict[str, int]] = None,
                              ) -> List[Dict[str, Any]]:
    """Restart a stalled leg, then reassign it through the common fleet loop.

    A navigation stall is an execution failure, not immediate evidence that
    the business task itself is impossible.  The first exhausted controller
    retry therefore retires the blocking actor and releases all of its
    unfinished work for one capability-safe reassignment.  Only an
    unrecoverable or repeatedly stalled task becomes terminal ``stuck``.
    """
    controls = []
    held = {str(item) for item in held_vehicle_ids or []}
    recovery_attempts = (
        recovery_attempts if recovery_attempts is not None else {}
    )
    navigation_recovery_attempts = progress.setdefault(
        "__navigation_recovery_attempts__", {}
    )
    for task in tasks:
        if task.status in TERMINAL_TASK_STATES or task.last_distance_m is None:
            continue
        if task.status == "assigned":
            # A reassignment may put the selected vehicle's original work
            # behind the urgent task.  That queued task is not navigating and
            # must not inherit the active vehicle's speed or be classified as
            # stalled.  Drop any old sample so it starts with a fresh
            # baseline when the queue later promotes it to execution.
            progress.pop(task.task_id, None)
            continue
        if task.assigned_vehicle_id and str(task.assigned_vehicle_id) in held:
            # A route-capacity hold is an intentional queueing state, not a
            # navigation attempt.  Keep any prior progress window fresh so
            # release does not immediately inherit waiting time as a stall.
            record = progress.get(task.task_id)
            if record is not None:
                record["last_progress_tick"] = tick_index
            continue
        distance = float(task.last_distance_m)
        # A task can contain several physical legs.  Production return,
        # reroute and takeover replace the navigation target and may make the
        # straight-line distance jump upward.  Give every new leg a fresh
        # progress baseline instead of inheriting the previous destination's
        # best distance and being falsely classified as stuck.
        progress_phase = (
            str(task.assigned_vehicle_id or ""),
            str(getattr(task, "status_reason", "") or ""),
        )
        record = progress.setdefault(task.task_id, {
            "initial_distance_m": distance,
            "best_distance_m": distance,
            "last_progress_tick": tick_index,
            "progress_m": 0.0,
            "restart_count": 0,
            "progress_phase": progress_phase,
        })
        if tuple(record.get("progress_phase") or ()) != progress_phase:
            record.update({
                "initial_distance_m": distance,
                "best_distance_m": distance,
                "last_progress_tick": tick_index,
                "progress_m": 0.0,
                "restart_count": 0,
                "progress_phase": progress_phase,
            })
            continue
        best = float(record.get("best_distance_m", distance))
        if distance <= best - STUCK_MIN_PROGRESS_M:
            record.update({
                "best_distance_m": distance,
                "last_progress_tick": tick_index,
                "progress_m": round(float(record["initial_distance_m"]) - distance, 3),
            })
            continue
        measured_speed = float(
            (vehicle_speed_mps or {}).get(
                str(task.assigned_vehicle_id or ""), 0.0
            ) or 0.0
        )
        if measured_speed >= STUCK_MIN_MOTION_SPEED_MPS:
            # A mine road can initially curve away from its destination.
            # Euclidean target distance may therefore increase while the
            # truck is correctly following its CARLA route.  Actor velocity
            # is factual motion evidence and prevents that normal traversal
            # from being labelled as a navigation stall.
            record.update({
                "last_progress_tick": tick_index,
                "motion_observed": True,
                "last_speed_mps": measured_speed,
            })
            continue
        if tick_index - int(record.get("last_progress_tick", tick_index)) < STUCK_WINDOW_TICKS:
            continue
        restart = callable(getattr(adapter, "set_task_speed_limit", None))
        if restart and int(record.get("restart_count", 0)) < MAX_NAVIGATION_RESTARTS:
            speed = float(task_speed_caps.get(task.task_id, 15.0))
            response = adapter.set_task_speed_limit(task.task_id, speed)
            record["restart_count"] = int(record.get("restart_count", 0)) + 1
            record["last_progress_tick"] = tick_index
            controls.append({
                "action": "restart_stalled_navigation",
                "status": "APPLIED", "task_id": task.task_id,
                "vehicle_id": task.assigned_vehicle_id,
                "stalled_distance_m": distance, **dict(response),
            })
            continue
        recovery_manoeuvre = getattr(
            adapter, "recover_task_navigation", None
        )
        if (
            callable(recovery_manoeuvre)
            and int(navigation_recovery_attempts.get(task.task_id, 0))
            < MAX_FORWARD_RECOVERY_MANEUVERS
        ):
            response = recovery_manoeuvre(task.task_id)
            if str(response.get("status")) == "APPLIED":
                navigation_recovery_attempts[task.task_id] = int(
                    navigation_recovery_attempts.get(task.task_id, 0)
                ) + 1
                progress.pop(task.task_id, None)
                controls.append({
                    "action": "start_forward_lane_navigation_recovery",
                    "status": "APPLIED",
                    "task_id": task.task_id,
                    "vehicle_id": task.assigned_vehicle_id,
                    "stalled_distance_m": distance,
                    **dict(response),
                })
                continue
        recovery_controls = _recover_stalled_vehicle_work(
            tasks, task, tick_index, adapter, progress,
            recovery_attempts, held,
        )
        if recovery_controls:
            controls.extend(recovery_controls)
            continue
        task.status = "stuck"
        task.status_reason = "no_recovery_vehicle_after_navigation_restart"
        task.completed_tick = tick_index
        retirement = getattr(adapter, "retire_vehicle", None)
        retirement_result = None
        if callable(retirement) and task.assigned_vehicle_id:
            retirement_result = retirement(
                task.assigned_vehicle_id,
                reason="navigation_stuck_clears_active_routes",
            )
        else:
            pause = getattr(adapter, "pause_vehicle", None)
            if callable(pause) and task.assigned_vehicle_id:
                pause(task.assigned_vehicle_id)
        control = {
            "action": "mark_task_stuck_safe_hold",
            "status": "APPLIED", "task_id": task.task_id,
            "vehicle_id": task.assigned_vehicle_id,
            "stalled_distance_m": distance,
        }
        if retirement_result is not None:
            control["vehicle_clearance"] = dict(retirement_result)
        controls.append(control)
    return controls


def _runtime_recovery_candidates(
    tasks: List[Any], task: Any, adapter: Any,
    excluded_vehicle_ids: Optional[List[str]] = None,
    held_vehicle_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Return capability-safe candidates using deterministic runtime facts.

    This is a safety fallback, not the project's optimization scheduler.  It
    applies hard feasibility filters and a stable tuple ordering only; the
    normal scheduler and multi-objective CostModel remain responsible for
    planned assignments.
    """
    excluded = {str(item) for item in excluded_vehicle_ids or []}
    held = {str(item) for item in held_vehicle_ids or []}
    states_method = getattr(adapter, "list_states", None)
    if not callable(states_method):
        return []
    try:
        states = list(states_method())
    except Exception:
        return []
    required = {str(item) for item in getattr(
        task, "required_capabilities", []
    )}
    active_by_vehicle: Dict[str, List[Any]] = {}
    for current in tasks:
        vehicle_id = str(getattr(current, "assigned_vehicle_id", "") or "")
        if vehicle_id and current.status not in TERMINAL_TASK_STATES:
            active_by_vehicle.setdefault(vehicle_id, []).append(current)
    candidates = []
    blocked_statuses = {
        "fault", "paused", "emergency_stop", "timed_out", "cancelled",
        "stuck", "retired_after_completion", "retired_after_execution_failure",
    }
    for state in states:
        vehicle_id = str(getattr(state, "vehicle_id", "") or "")
        capabilities = {
            str(item) for item in getattr(state, "capabilities", []) or []
        }
        status = str(getattr(state, "task_status", "") or "")
        vehicle_tasks = active_by_vehicle.get(vehicle_id, [])
        current_priority = max([
            int(getattr(item, "priority", 0) or 0)
            for item in vehicle_tasks
        ] or [0])
        reasons = []
        if not vehicle_id or vehicle_id in excluded:
            reasons.append("excluded_vehicle")
        if vehicle_id in held:
            reasons.append("held_by_safety_guard")
        if not bool(getattr(state, "available", True)):
            reasons.append("vehicle_unavailable")
        if str(getattr(state, "health", "healthy")) != "healthy":
            reasons.append("vehicle_unhealthy")
        if status in blocked_statuses:
            reasons.append("vehicle_state_not_executable")
        if not required.issubset(capabilities):
            reasons.append("capability_mismatch")
        if current_priority > int(getattr(task, "priority", 0) or 0):
            reasons.append("higher_priority_work_in_progress")
        candidates.append({
            "vehicle_id": vehicle_id,
            "feasible": not reasons,
            "constraint_results": reasons,
            "active_task_count": len(vehicle_tasks),
            "current_max_priority": current_priority,
            "state": status,
        })
    return sorted(
        candidates,
        key=lambda item: (
            not item["feasible"],
            item["active_task_count"],
            item["current_max_priority"],
            item["vehicle_id"],
        ),
    )


def _recover_stalled_vehicle_work(
    tasks: List[Any], stalled_task: Any, tick_index: int, adapter: Any,
    progress: Dict[str, Dict[str, Any]], recovery_attempts: Dict[str, int],
    held_vehicle_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Release and reassign unfinished work owned by one stalled actor."""
    reassign = getattr(adapter, "reassign_task", None)
    retire = getattr(adapter, "retire_vehicle", None)
    stalled_vehicle_id = str(
        getattr(stalled_task, "assigned_vehicle_id", "") or ""
    )
    if not stalled_vehicle_id or not callable(reassign) or not callable(retire):
        return []
    affected = [
        item for item in tasks
        if str(getattr(item, "assigned_vehicle_id", "") or "")
        == stalled_vehicle_id
        and item.status not in TERMINAL_TASK_STATES
    ]
    if not affected:
        return []
    # If the triggering task already consumed its one fleet-level retry, the
    # caller must terminate it rather than creating an endless reassignment
    # cycle.
    if int(recovery_attempts.get(stalled_task.task_id, 0)) >= \
            MAX_RUNTIME_TASK_REASSIGNMENTS:
        return []

    retirement = retire(
        stalled_vehicle_id,
        reason="navigation_stall_releases_unfinished_work",
    )
    controls = [{
        "action": "retire_stalled_vehicle_for_task_recovery",
        "status": "APPLIED",
        "vehicle_id": stalled_vehicle_id,
        "trigger_task_id": stalled_task.task_id,
        "vehicle_clearance": dict(retirement),
    }]
    # Higher-priority work is considered first; candidate load is recomputed
    # after every assignment so multiple released tasks spread across the
    # remaining fleet where possible.
    for released_task in sorted(
        affected,
        key=lambda item: (-int(getattr(item, "priority", 0) or 0), item.task_id),
    ):
        task_id = str(released_task.task_id)
        old_vehicle_id = str(released_task.assigned_vehicle_id or "")
        released_task.original_vehicle_id = (
            released_task.original_vehicle_id or old_vehicle_id
        )
        released_task.assigned_vehicle_id = None
        released_task.status = "released"
        released_task.status_reason = "released_after_runtime_navigation_stall"
        released_task.completed_tick = None
        released_task.handover_reason = "runtime_navigation_stall"
        released_task.handover_tick = int(tick_index)
        progress.pop(task_id, None)

        candidates = _runtime_recovery_candidates(
            tasks, released_task, adapter,
            excluded_vehicle_ids=[stalled_vehicle_id],
            held_vehicle_ids=list(held_vehicle_ids or []),
        )
        selected = next(
            (item for item in candidates if item["feasible"]), None
        )
        if selected is None:
            released_task.status = "stuck"
            released_task.status_reason = "no_capability_safe_recovery_vehicle"
            released_task.completed_tick = int(tick_index)
            controls.append({
                "action": "mark_released_task_stuck_no_recovery_vehicle",
                "status": "NO_FEASIBLE_CANDIDATE",
                "task_id": task_id,
                "old_vehicle_id": old_vehicle_id,
                "candidate_evaluations": candidates,
            })
            continue

        response = _reassign_for_scenario(
            adapter, task_id, selected["vehicle_id"], "scenario_recovery"
        )
        navigation_recovery_attempts = progress.get(
            "__navigation_recovery_attempts__", {}
        )
        if isinstance(navigation_recovery_attempts, dict):
            navigation_recovery_attempts.pop(task_id, None)
        recovery_attempts[task_id] = int(
            recovery_attempts.get(task_id, 0)
        ) + 1
        control = {
            "action": "reassign_after_runtime_navigation_stall",
            "status": "APPLIED",
            "task_id": task_id,
            "old_vehicle_id": old_vehicle_id,
            "vehicle_id": selected["vehicle_id"],
            "recovery_attempt": recovery_attempts[task_id],
            "selection_policy": "HARD_CONSTRAINTS_THEN_RUNTIME_LOAD",
            "candidate_evaluations": candidates,
            **dict(response),
        }
        controls.append(control)
        _emit(adapter, "runtime_stalled_task_reassigned", control)
    return controls


def _recently_progressing_task_ids(
    tasks: List[Any], tick_index: int,
    progress: Dict[str, Dict[str, Any]],
) -> List[str]:
    """Return non-terminal tasks with recent measured route progress."""
    task_ids = []
    for task in tasks:
        if task.status in TERMINAL_TASK_STATES or task.status == "assigned":
            continue
        record = progress.get(task.task_id) or {}
        # A freshly observed navigation leg may not yet have accumulated the
        # five-metre progress quantum.  It is still allowed one watchdog
        # window; the per-task watchdog will restart and then terminate it if
        # no real progress follows.
        if record.get("initial_distance_m") is None:
            continue
        last_progress_tick = record.get("last_progress_tick")
        if last_progress_tick is None:
            continue
        if tick_index - int(last_progress_tick) <= STUCK_WINDOW_TICKS:
            task_ids.append(str(task.task_id))
    return sorted(task_ids)


def _emit(adapter: Any, event_type: str, payload: Dict[str, Any]) -> None:
    emitter = getattr(adapter, "_emit", None)
    if callable(emitter):
        emitter(event_type, payload)


def _runtime_adapter_call(callback, *args, **kwargs):
    """Keep third-party CARLA navigation chatter out of result JSON stdout."""
    with redirect_stdout(sys.stderr):
        return callback(*args, **kwargs)


def _reassign_for_scenario(adapter: Any, task_id: str, vehicle_id: str,
                           assignment_source: str) -> Dict[str, Any]:
    """Use queue-front reassignment while retaining old adapter compatibility.

    The bundled CarlaAdapter accepts ``assignment_source``.  Small test
    adapters and external compatibility adapters written before this contract
    may not, so they still receive the same task/vehicle operation without
    misrepresenting their result as a new capability.
    """
    method = adapter.reassign_task
    try:
        return method(
            task_id, vehicle_id, assignment_source=assignment_source
        )
    except TypeError as exc:
        if "assignment_source" not in str(exc):
            raise
        return method(task_id, vehicle_id)


def _apply_primary_event(scenario: str, structural: Dict[str, Any],
                         workload: Dict[str, Any], tasks: List[Any],
                         adapter: Any,
                         route_waypoints: Optional[
                             Dict[Tuple[str, ...], List[Dict[str, float]]]
                         ] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Translate approved structural decisions into adapter operations."""
    applied, paused = [], []
    task_by_id = {item.task_id: item for item in tasks}
    route_waypoints = route_waypoints or {}
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
                _reassign_for_scenario(
                    adapter, str(task_id), selected, "scenario_event"
                )
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
            route_response = _apply_selected_route(
                adapter, task_id, str(decision.get("vehicle_id")),
                decision, route_waypoints,
            )
            if route_response:
                record("apply_equipment_alternative_route", dict(route_response))

    if scenario == "s04":
        for decision in structural.get("blast_decisions", []):
            if decision.get("action_type") == "hold_until_blast_clearance":
                vehicle_id = str(decision.get("vehicle_id"))
                paused.append(vehicle_id)
                record("hold_for_blast_clearance", dict(adapter.pause_vehicle(vehicle_id)))
            elif decision.get("vehicle_id") and decision.get("task_id"):
                task = task_by_id.get(str(decision.get("task_id")))
                if task is not None and task.status not in TERMINAL_TASK_STATES:
                    record("blast_zone_safe_route", dict(_reassign_for_scenario(
                        adapter, task.task_id, str(decision.get("vehicle_id")),
                        "scenario_event"
                    )))
                    route_response = _apply_selected_route(
                        adapter, task.task_id, str(decision.get("vehicle_id")),
                        decision, route_waypoints,
                    )
                    if route_response:
                        record("apply_blast_safe_route", dict(route_response))

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
            route_response = _apply_selected_route(
                adapter, task_id, str(decision.get("vehicle_id")),
                decision, route_waypoints,
            )
            if route_response:
                record("apply_weather_alternative_route", dict(route_response))

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
            response = dict(_reassign_for_scenario(
                adapter, task_id, str(selected), "scenario_event"
            ))
            record(str(decision.get("action_type") or "replan_task"), response)
            original_vehicle_id = str(
                decision.get("original_vehicle_id") or ""
            )
            if (
                original_vehicle_id
                and original_vehicle_id != str(selected)
                and callable(getattr(adapter, "retire_vehicle", None))
            ):
                clearance = dict(adapter.retire_vehicle(
                    original_vehicle_id,
                    reason="road_closure_takeover_clears_active_route",
                ))
                record("clear_displaced_vehicle_after_route_takeover", {
                    "task_id": task_id,
                    "vehicle_id": original_vehicle_id,
                    "replacement_vehicle_id": str(selected),
                    "vehicle_clearance": clearance,
                })
            route_response = _apply_selected_route(
                adapter, task_id, str(selected), decision, route_waypoints,
            )
            if route_response:
                record("apply_selected_road_graph_route", dict(route_response))
    return applied, paused


def _apply_recovery(scenario: str, structural: Dict[str, Any],
                    workload: Dict[str, Any], tasks: List[Any], adapter: Any,
                    paused: List[str], route_waypoints: Optional[
                        Dict[Tuple[str, ...], List[Dict[str, float]]]
                    ] = None) -> List[Dict[str, Any]]:
    applied = []
    route_waypoints = route_waypoints or {}
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
            response = _reassign_for_scenario(
                adapter, str(task_id), selected, "scenario_recovery"
            )
            applied.append({
                "action": "compound_fault_task_takeover",
                "status": "APPLIED", **response,
            })
            takeover = structural.get("takeover_decision") or {}
            if str(takeover.get("task_id")) == str(task_id):
                route_response = _apply_selected_route(
                    adapter, str(task_id), str(selected), takeover,
                    route_waypoints,
                )
                if route_response:
                    applied.append({
                        "action": "apply_compound_takeover_route",
                        "status": "APPLIED", **route_response,
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
                                 runtime_publisher: Optional[Callable[[Dict[str, Any]], None]] = None,
                                 operator_reviewer: Optional[Callable[[Dict[str, Any]], str]] = None,
                                 control_state_reader: Optional[Callable[[], str]] = None,
                                 ) -> Dict[str, Any]:
    """Execute S01-S07/S09 through one CARLA and feedback contract."""
    name = str(scenario).lower()
    if name not in CARLA_SCENARIOS:
        raise ValueError("unsupported CARLA scenario: {}".format(scenario))
    if ticks < 1:
        raise ValueError("ticks must be at least 1")
    from .runner import run_structural_scenario
    common_spec = scenario_spec(name, load_scenario_catalog()).to_dict()

    lifecycle = ScenarioLifecycle()
    lifecycle.mark("prepare", "completed", {
        "scenario_key": name, "seed": seed, "vehicle_count": vehicle_count,
        "check_only": bool(check_only),
    })
    policy = execution_policy
    if policy == "auto":
        policy = "multi-objective" if name in {"s01", "s02"} else "heuristic"
    binding = config.map_resource
    physical_validation_records = []
    with MapResourceStore(binding.database_path) as resource_store:
        planner_pairs = {
            (str(item["from_point_id"]), str(item["to_point_id"]))
            for item in resource_store.planner_reachable_pairs(
                binding.map_id, binding.resource_version
            )
        }
        if physical_route_pairs is None:
            physical_validation_records = list(
                resource_store.physical_route_validations(
                    binding.map_id, binding.resource_version
                )
            )
            physical_pairs = {
                (str(item["from_point_id"]), str(item["to_point_id"]))
                for item in physical_validation_records
                if item.get("validation_status") == "PHYSICAL_REACHED"
            }
    if physical_route_pairs is not None:
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
    try:
        structural = run_structural_scenario(
            name, config, seed=seed, random_map=True,
            vehicle_count=vehicle_count, execution_policy=policy,
            eligible_pairs=physical_pairs,
            minimum_length_m=100.0, maximum_length_m=1000.0,
        )
    except ValueError as exc:
        raise ValueError(
            "P6_ROUTE_ADMISSION_FAILED: {} 在当前P6路线集中无法生成"
            "同时满足地图、任务和事件硬约束的{}车工况；"
            "已在CARLA启动前拒绝，不会退回P5执行。原因：{}: {}".format(
                name.upper(), vehicle_count, type(exc).__name__, exc
            )
        ) from exc
    workload_seed = structural.get("workload_seed", structural.get("seed", seed))
    workload = prepare_random_map_workload(
        config, seed=workload_seed, vehicle_count=vehicle_count,
        minimum_length_m=100.0, maximum_length_m=1000.0,
        scenario_key=name,
        eligible_pairs=physical_pairs,
        deadhead_eligible_pairs=physical_pairs,
    )
    initial_assignments = _assignment_map(
        structural.get("initial_assignments") or structural.get("assignments")
    )
    # Prefer the final structural map for runtime budgeting because it already
    # includes a known scenario takeover/reassignment queue (for example S02).
    final_assignments = _assignment_map(structural.get("tasks", []))
    task_mission_plans = _task_mission_plans(
        workload, physical_pairs, planner_pairs,
        initial_assignments or final_assignments,
    )
    physical_route_gate = _validate_physical_route_admission(
        task_mission_plans, physical_pairs
    )
    route_evidence_admission = {
        "status": "P6_PHYSICAL_REACHED_REQUIRED",
        "required_evidence": "P6_PHYSICAL_REACHED",
        "selected_evidence": "P6_PHYSICAL_REACHED",
        "fallback_allowed": False,
        "physical_prevalidated": True,
        "requires_carla_result_promotion": False,
        "task_route_gate": physical_route_gate,
        "boundary": (
            "Every initial mission/deadhead leg has isolated P6 success "
            "evidence. This is not multi-vehicle collision, clearance or "
            "traffic-safety validation."
        ),
    }
    task_route_edges = _load_task_route_edges(workload)
    launch_schedule = _fleet_launch_schedule(
        workload, route_edge_plans=task_route_edges,
        physical_route_capacity=P6_DEFAULT_CONCURRENT_ROUTE_CAPACITY,
    )
    physical_speed_caps, physical_speed_evidence = _physical_speed_caps(
        workload, physical_validation_records
    )
    production_cycle_plans = _production_cycle_plans(
        workload, physical_pairs, planner_pairs,
        assignments=initial_assignments or final_assignments,
    )
    runtime_route_waypoints = _runtime_route_waypoints(
        workload["config"], structural
    )
    recommended_ticks, tick_budget = _recommended_execution_ticks(
        workload, name, physical_validation_records, launch_schedule,
        assignments=final_assignments or initial_assignments,
        # Route-edge conflict admission can serialize part of the fleet.  A
        # serial budget prevents the safety watchdog from ending a valid
        # queued episode merely because concurrency was deliberately reduced.
        physical_route_capacity=1,
    )
    requested_ticks = int(ticks)
    # This is a last-resort safety watchdog.  Successful episodes end because
    # their tasks and incident lifecycle are complete, not because this count
    # was reached.
    production_recommended_ticks = (
        recommended_ticks * 2
        + (CARLA_LOADING_SERVICE_TICKS + CARLA_DUMPING_SERVICE_TICKS)
        * max(1, int(vehicle_count))
    )
    effective_execution_ticks = max(requested_ticks, production_recommended_ticks)
    configured_timeout_ticks = int(workload["config"].demo.task_timeout_ticks)
    effective_timeout_ticks = max(configured_timeout_ticks, effective_execution_ticks)
    if effective_timeout_ticks != configured_timeout_ticks:
        workload["config"] = replace(
            workload["config"],
            demo=replace(
                workload["config"].demo,
                task_timeout_ticks=effective_timeout_ticks,
            ),
        )
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
    events, feedback, controls, runtime_telemetry = [], [], [], []
    ticks_executed, final_states, destroyed = 0, [], 0
    scheduled_event_tick, scheduled_recovery_tick = _event_ticks(
        name, structural, effective_execution_ticks
    )
    event_tick, recovery_tick = None, None
    recovery_delay_ticks = max(
        0, int(scheduled_recovery_tick or 0) - int(scheduled_event_tick or 0)
    )
    paused, event_applied, recovery_applied = [], False, False
    operator_review_status = "NOT_REQUESTED"
    progress = {
        "__failed_vehicle__": {
            "vehicle_id": structural.get("failed_vehicle_id"),
        }
    }
    runtime_recovery_attempts: Dict[str, int] = {}
    _runtime_adapter_call(adapter.connect)
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
                "scheduled_event_tick": scheduled_event_tick,
                "scheduled_recovery_tick": scheduled_recovery_tick,
                "event_tick": None, "recovery_tick": None,
                "configured_task_timeout_ticks": configured_timeout_ticks,
                "effective_task_timeout_ticks": effective_timeout_ticks,
                "ticks_requested": requested_ticks,
                "effective_execution_ticks": effective_execution_ticks,
                "minimum_recommended_ticks": recommended_ticks,
                "production_recommended_ticks": production_recommended_ticks,
                "production_cycle_plans": production_cycle_plans,
                "task_mission_plans": task_mission_plans,
                "mission_configuration": {
                    "status": "PLANNED_CHECK_ONLY",
                    "task_count": len(task_mission_plans),
                    "deadhead_task_count": sum(
                        bool(item.get("requires_deadhead"))
                        for item in task_mission_plans
                    ),
                    "execution_model": (
                        "SPAWN_TO_SERVICE_ORIGIN_TO_SERVICE_TARGET"
                    ),
                },
                "tick_budget": tick_budget,
                "launch_schedule": launch_schedule,
                "route_traffic_admission": {
                    "status": "READY",
                    "physical_concurrent_route_capacity": None,
                    "capacity_source": "NOT_MULTI_TRUCK_VALIDATED",
                    "runtime_admission_strategy": (
                        "TOPOLOGY_CONFLICT_SERIALIZED_WITH_HEADWAY"
                    ),
                    "topology_route_count": len(task_route_edges),
                },
                "runtime_traffic_coordination": _runtime_traffic_summary(
                    {}, supported=(
                        callable(getattr(adapter, "pause_vehicle", None))
                        and callable(getattr(adapter, "resume_vehicle", None))
                        and callable(getattr(adapter, "list_states", None))
                    ), check_only=True,
                ),
                "route_evidence_admission": route_evidence_admission,
                "runtime_route_sequence_count": len(runtime_route_waypoints),
                "physical_speed_caps": physical_speed_evidence,
                "spawn_point_indices": [item.spawn_point_index for item in workload["vehicles"]],
                "target_spawn_point_indices": [item.target_spawn_point_index for item in workload["zones"]],
                "simulation_claim": "carla_connection_and_workload_admission_only",
                "execution_commands": [command.to_dict()], "execution_feedback": [],
            }
            lifecycle.mark("finish", "READY", {})
            result["lifecycle"] = lifecycle.to_dict()
            return normalize_scenario_run_result(
                result, scenario_key=name, scenario_spec=common_spec
            )
        _runtime_adapter_call(adapter.ensure_vehicles, spawn_missing=True)
        runtime_zones = _runtime_adapter_call(adapter.resolve_zones, workload["zones"])
        mission_supported = callable(
            getattr(adapter, "configure_task_missions", None)
        )
        if mission_supported:
            mission_configuration = _runtime_adapter_call(
                adapter.configure_task_missions, task_mission_plans
            )
        else:
            mission_configuration = {
                "status": "NOT_SUPPORTED_BY_ADAPTER", "task_count": 0,
            }
        production_supported = callable(
            getattr(adapter, "configure_production_cycles", None)
        )
        if production_supported:
            production_configuration = _runtime_adapter_call(
                adapter.configure_production_cycles, production_cycle_plans
            )
        else:
            production_configuration = {
                "status": "NOT_SUPPORTED_BY_ADAPTER", "task_count": 0,
            }
        dispatch_feedback = _runtime_adapter_call(
            manager.dispatch, command, tasks, runtime_zones
        )
        feedback.append(dispatch_feedback.to_dict())
        if dispatch_feedback.status != "SUCCEEDED":
            raise RuntimeError(dispatch_feedback.error or "CARLA dispatch failed")
        events.extend(dispatch_feedback.events)
        _publish_runtime_snapshot(
            runtime_publisher, adapter, tasks, workload, structural, name, 0,
            "dispatch", event={
                "type": "scenario_dispatch",
                "message": "{} 多车任务已下发，等待CARLA执行反馈".format(name.upper()),
            },
        )
        speed_cap_supported = callable(getattr(adapter, "set_task_speed_limit", None))
        arrival_tolerance_supported = callable(
            getattr(adapter, "set_task_arrival_tolerance", None)
        )
        physical_evidence_by_task = {
            str(item["task_id"]): item for item in physical_speed_evidence
        }
        if speed_cap_supported:
            for task_id, speed_limit_kmh in sorted(physical_speed_caps.items()):
                response = _runtime_adapter_call(
                    adapter.set_task_speed_limit, task_id, speed_limit_kmh
                )
                controls.append({
                    "action": "apply_p6_validated_speed_cap",
                    "status": "APPLIED",
                    "source": "P6_ISOLATED_SINGLE_TRUCK_VALIDATION",
                    **response,
                })
                tolerance = physical_evidence_by_task.get(task_id, {}).get(
                    "arrival_tolerance_m"
                )
                if arrival_tolerance_supported and tolerance is not None:
                    tolerance_response = _runtime_adapter_call(
                        adapter.set_task_arrival_tolerance,
                        task_id, tolerance,
                    )
                    controls.append({
                        "action": "apply_p6_validated_arrival_tolerance",
                        "status": "APPLIED",
                        **tolerance_response,
                    })
        headway_supported = callable(getattr(adapter, "pause_vehicle", None)) and callable(
            getattr(adapter, "resume_vehicle", None)
        )
        route_admission_state = {
            "active_task_id": None,
            "active_vehicle_id": None,
            "held_vehicle_ids": [],
        }
        traffic_coordination_state: Dict[str, Any] = {
            "holds": {}, "observations": {}, "decisions": [],
            "detected_conflict_count": 0,
            "resolved_conflict_count": 0,
            "escalated_conflict_count": 0,
        }
        traffic_coordination_supported = (
            headway_supported
            and callable(getattr(adapter, "list_states", None))
        )
        if headway_supported:
            controls.extend(_runtime_adapter_call(
                _reconcile_route_admission, tasks, adapter, launch_schedule,
                route_admission_state, [], 0,
            ))
        tick_index = 0
        safety_watchdog_triggered = False
        safety_watchdog_extension_count = 0
        operator_pause_active = False
        operator_paused_vehicle_ids = set()
        while not all(task.status in TERMINAL_TASK_STATES for task in tasks):
            requested_control_state = (
                str(control_state_reader()).lower()
                if control_state_reader is not None else "running"
            )
            if requested_control_state == "paused":
                if not operator_pause_active:
                    preexisting_holds = set(
                        route_admission_state.get("held_vehicle_ids", [])
                    ) | set(traffic_coordination_state.get("holds", {})) | {
                        str(item) for item in paused
                    }
                    for state in _runtime_adapter_call(adapter.list_states):
                        vehicle_id = str(getattr(state, "vehicle_id", ""))
                        task_status = str(getattr(state, "task_status", ""))
                        if not vehicle_id or task_status in {
                            "fault", "paused", "emergency_stop", "idle",
                            "completed", "timed_out", "cancelled", "stuck",
                        }:
                            continue
                        _runtime_adapter_call(adapter.pause_vehicle, vehicle_id)
                        if vehicle_id not in preexisting_holds:
                            operator_paused_vehicle_ids.add(vehicle_id)
                    pause_event = {
                        "event_type": "scenario_paused",
                        "tick": tick_index,
                        "payload": {
                            "source": "dispatch_center",
                            "vehicle_ids": sorted(operator_paused_vehicle_ids),
                        },
                    }
                    events.append(pause_event)
                    controls.append({
                        "action": "pause_scenario",
                        "status": "APPLIED",
                        "tick": tick_index,
                        "vehicle_ids": sorted(operator_paused_vehicle_ids),
                    })
                    operator_pause_active = True
                    _publish_runtime_snapshot(
                        runtime_publisher, adapter, tasks, workload,
                        structural, name, tick_index, "paused", event={
                            "type": "scenario_paused",
                            "message": "场景已暂停，车辆保持安全制动",
                        },
                    )
                # Scenario time and task watchdogs do not advance while the
                # operator has paused the episode.
                time.sleep(0.1)
                continue
            if operator_pause_active:
                protected_holds = set(
                    route_admission_state.get("held_vehicle_ids", [])
                ) | set(traffic_coordination_state.get("holds", {})) | {
                    str(item) for item in paused
                }
                resumed_vehicle_ids = []
                for vehicle_id in sorted(operator_paused_vehicle_ids):
                    if vehicle_id in protected_holds:
                        continue
                    _runtime_adapter_call(adapter.resume_vehicle, vehicle_id)
                    resumed_vehicle_ids.append(vehicle_id)
                events.append({
                    "event_type": "scenario_resumed",
                    "tick": tick_index,
                    "payload": {
                        "source": "dispatch_center",
                        "vehicle_ids": resumed_vehicle_ids,
                    },
                })
                controls.append({
                    "action": "resume_scenario",
                    "status": "APPLIED",
                    "tick": tick_index,
                    "vehicle_ids": resumed_vehicle_ids,
                })
                operator_paused_vehicle_ids.clear()
                operator_pause_active = False
                _publish_runtime_snapshot(
                    runtime_publisher, adapter, tasks, workload, structural,
                    name, tick_index, "execute", event={
                        "type": "scenario_resumed",
                        "message": "场景已继续执行",
                    },
                )
            if tick_index >= effective_execution_ticks:
                progressing_task_ids = _recently_progressing_task_ids(
                    tasks, tick_index, progress
                )
                if progressing_task_ids:
                    previous_limit = effective_execution_ticks
                    effective_execution_ticks += SAFETY_WATCHDOG_EXTENSION_TICKS
                    effective_timeout_ticks = max(
                        effective_timeout_ticks, effective_execution_ticks
                    )
                    # CarlaAdapter reads its timeout from the current config
                    # on every tick.  Keep that legacy absolute guard behind
                    # the scenario progress watchdog during an extension.
                    adapter_config = getattr(adapter, "config", None)
                    adapter_demo = getattr(adapter_config, "demo", None)
                    if adapter_demo is not None:
                        adapter.config = replace(
                            adapter_config,
                            demo=replace(
                                adapter_demo,
                                task_timeout_ticks=effective_timeout_ticks,
                            ),
                        )
                    safety_watchdog_extension_count += 1
                    controls.append({
                        "action": "extend_safety_watchdog_for_measured_progress",
                        "status": "APPLIED",
                        "tick": tick_index,
                        "previous_tick_limit": previous_limit,
                        "new_tick_limit": effective_execution_ticks,
                        "progressing_task_ids": progressing_task_ids,
                    })
                    continue
                safety_watchdog_triggered = True
                capacity_held = {
                    str(item) for item in route_admission_state.get(
                        "held_vehicle_ids", []
                    )
                } if headway_supported else set()
                for task in tasks:
                    if task.status in TERMINAL_TASK_STATES:
                        continue
                    was_capacity_held = bool(
                        task.assigned_vehicle_id
                        and str(task.assigned_vehicle_id) in capacity_held
                    )
                    task.status = "timed_out" if was_capacity_held else "stuck"
                    task.status_reason = (
                        "route_capacity_hold_at_safety_watchdog_limit"
                        if was_capacity_held
                        else "scenario_safety_watchdog_tick_limit"
                    )
                    task.completed_tick = tick_index
                    pause = getattr(adapter, "pause_vehicle", None)
                    if callable(pause) and task.assigned_vehicle_id:
                        _runtime_adapter_call(pause, task.assigned_vehicle_id)
                    controls.append({
                        "action": (
                            "route_capacity_hold_timed_out"
                            if was_capacity_held
                            else "scenario_safety_watchdog_terminated_task"
                        ),
                        "status": "APPLIED", "task_id": task.task_id,
                        "vehicle_id": task.assigned_vehicle_id,
                    })
                break
            if (scheduled_event_tick and not event_applied and _event_is_ready(
                    name, scheduled_event_tick, tick_index, launch_schedule,
                    tasks, progress, requested_ticks)):
                event_tick = tick_index
                if recovery_delay_ticks:
                    recovery_tick = event_tick + recovery_delay_ticks
                review_paused = []
                review_points = _event_review_points(structural)
                if operator_reviewer is not None and review_points:
                    # The safe default while a human is considering an
                    # incident response is to hold this small demonstration
                    # fleet.  No response means no event action is executed;
                    # the episode then ends through normal actor cleanup.
                    for state in _runtime_adapter_call(adapter.list_states):
                        vehicle_id = str(getattr(state, "vehicle_id", ""))
                        if vehicle_id:
                            _runtime_adapter_call(adapter.pause_vehicle, vehicle_id)
                            review_paused.append(vehicle_id)
                    pending_points = []
                    for point in review_points:
                        pending = dict(point)
                        pending["review_status"] = "PENDING_HUMAN_CONFIRMATION"
                        pending["operator_response"] = None
                        pending_points.append(pending)
                    _publish_runtime_snapshot(
                        runtime_publisher, adapter, tasks, workload, structural,
                        name, tick_index, "await_human_review", event={
                            "type": "decision_point_pending",
                            "message": "{} 事件响应等待调度员确认".format(name.upper()),
                            "payload": {"decision_point_ids": [
                                item["decision_point_id"] for item in pending_points
                            ]},
                        }, decision_override={
                            "status": "PENDING_HUMAN_CONFIRMATION",
                            "policy_version": structural.get("policy_version"),
                            "decision_points": pending_points,
                        }
                    )
                    responses = [
                        str(operator_reviewer(point)).lower().strip()
                        for point in pending_points
                    ]
                    operator_review_status = (
                        "APPROVED_BY_HUMAN" if responses and all(
                            item == "approve" for item in responses
                        ) else "REJECTED_OR_TIMEOUT"
                    )
                    if operator_review_status != "APPROVED_BY_HUMAN":
                        controls.append({
                            "action": "hold_for_unapproved_decision_point",
                            "status": operator_review_status,
                            "decision_point_ids": [
                                item["decision_point_id"] for item in pending_points
                            ],
                        })
                        events.append({
                            "event_type": "decision_point_not_approved",
                            "payload": {"responses": responses},
                        })
                        break
                    traffic_held = set(
                        route_admission_state.get("held_vehicle_ids", [])
                    ) | set(traffic_coordination_state.get("holds", {})) \
                        if headway_supported else set()
                    for vehicle_id in review_paused:
                        # Human approval releases the incident hold, but it
                        # must not bypass the independent route-capacity hold.
                        if vehicle_id not in traffic_held:
                            _runtime_adapter_call(adapter.resume_vehicle, vehicle_id)
                current, paused = _runtime_adapter_call(
                    _apply_primary_event, name, structural, workload, tasks,
                    adapter, runtime_route_waypoints,
                )
                controls.extend(current)
                event_applied = True
                if headway_supported:
                    controls.extend(_runtime_adapter_call(
                        _reconcile_route_admission, tasks, adapter,
                        launch_schedule, route_admission_state, paused,
                        tick_index,
                    ))
                _publish_runtime_snapshot(
                    runtime_publisher, adapter, tasks, workload, structural, name,
                    tick_index, "event", event={
                        "type": "scenario_event_applied",
                        "message": "{} 场景事件已触发，调度响应已进入执行".format(name.upper()),
                        "payload": {"controls": current},
                    },
                )
            if recovery_tick and tick_index == recovery_tick and not recovery_applied:
                recovery_controls = _runtime_adapter_call(
                    _apply_recovery, name, structural, workload, tasks,
                    adapter, paused, runtime_route_waypoints,
                )
                controls.extend(recovery_controls)
                recovery_applied = True
                if headway_supported:
                    controls.extend(_runtime_adapter_call(
                        _reconcile_route_admission, tasks, adapter,
                        launch_schedule, route_admission_state, [],
                        tick_index,
                    ))
                _publish_runtime_snapshot(
                    runtime_publisher, adapter, tasks, workload, structural, name,
                    tick_index, "recovery", event={
                        "type": "scenario_recovery_applied",
                        "message": "{} 场景恢复控制已执行".format(name.upper()),
                        "payload": {"controls": recovery_controls},
                    },
                )
            _runtime_adapter_call(adapter.tick)
            ticks_executed = tick_index + 1
            events.extend(adapter.drain_events())
            if headway_supported:
                controls.extend(_runtime_adapter_call(
                    _reconcile_route_admission, tasks, adapter,
                    launch_schedule, route_admission_state, paused,
                    ticks_executed,
                ))
            if (
                traffic_coordination_supported
                and (
                    ticks_executed == 1
                    or ticks_executed % TRAFFIC_COORDINATION_INTERVAL_TICKS == 0
                )
            ):
                traffic_protected = set(
                    route_admission_state.get("held_vehicle_ids", [])
                ) | set(operator_paused_vehicle_ids)
                if not recovery_applied:
                    traffic_protected.update(str(item) for item in paused)
                traffic_states = list(_runtime_adapter_call(adapter.list_states))
                controls.extend(_runtime_adapter_call(
                    _reconcile_runtime_traffic, tasks, traffic_states,
                    adapter, traffic_coordination_state, ticks_executed,
                    sorted(traffic_protected),
                ))
                events.extend(adapter.drain_events())
            if (
                tick_index == 0
                or (ticks_executed % RUNTIME_TELEMETRY_INTERVAL_TICKS) == 0
                or all(task.status in TERMINAL_TASK_STATES for task in tasks)
            ):
                runtime_sample = _runtime_adapter_call(
                    _fleet_runtime_sample, adapter, tasks, ticks_executed
                )
                runtime_telemetry.append(runtime_sample)
                measured_vehicle_speeds = {
                    str(item.get("assigned_vehicle_id")): float(
                        (item.get("vehicle") or {}).get("speed_mps") or 0.0
                    )
                    for item in runtime_sample.get("tasks", [])
                    if item.get("assigned_vehicle_id")
                }
                watchdog_controls = _runtime_adapter_call(
                    _update_progress_watchdog, tasks, ticks_executed, adapter,
                    progress, physical_speed_caps,
                    sorted(
                        set(route_admission_state.get("held_vehicle_ids", []))
                        | set(traffic_coordination_state.get("holds", {}))
                    ) if headway_supported else [],
                    measured_vehicle_speeds,
                    runtime_recovery_attempts,
                )
                controls.extend(watchdog_controls)
                _publish_runtime_snapshot(
                    runtime_publisher, adapter, tasks, workload, structural, name,
                    ticks_executed, "execute"
                )
            tick_index += 1
        events.extend(adapter.drain_events())
        final_states = vehicle_state_snapshot(adapter.list_states())
        terminal_status = "SUCCEEDED" if all(item.status == "completed" for item in tasks) else "PARTIAL"
        feedback.append(manager.observe(
            command, tasks, phase="terminal", status=terminal_status, events=events
        ).to_dict())
        _publish_runtime_snapshot(
            runtime_publisher, adapter, tasks, workload, structural, name,
            ticks_executed, "finish", outcome={
                "status": terminal_status,
                "completed_task_count": sum(item.status == "completed" for item in tasks),
                "task_count": len(tasks),
            }
        )
    finally:
        if not check_only:
            destroyed = _runtime_adapter_call(adapter.destroy_spawned_vehicles)
        _runtime_adapter_call(adapter.close)

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
        "ticks_requested": requested_ticks,
        "effective_execution_ticks": effective_execution_ticks,
        "minimum_recommended_ticks": recommended_ticks,
        "production_recommended_ticks": production_recommended_ticks,
        "production_cycle_plans": production_cycle_plans,
        "task_mission_plans": task_mission_plans,
        "mission_configuration": mission_configuration,
        "production_configuration": production_configuration,
        "production_runtime_status": (
            "CARLA_PRODUCTION_CYCLE_EXECUTED"
            if production_configuration.get("status") == "CONFIGURED"
            else "LEGACY_POINT_TO_POINT_ADAPTER"
        ),
        "tick_budget": tick_budget,
        "ticks_executed": ticks_executed,
        "all_tasks_completed": completed == len(tasks),
        "scheduled_event_tick": scheduled_event_tick,
        "scheduled_recovery_tick": scheduled_recovery_tick,
        "event_tick": event_tick, "recovery_tick": recovery_tick,
        "configured_task_timeout_ticks": configured_timeout_ticks,
        "effective_task_timeout_ticks": effective_timeout_ticks,
        "safety_watchdog_tick_limit": effective_execution_ticks,
        "safety_watchdog_triggered": safety_watchdog_triggered,
        "safety_watchdog_extension_count": safety_watchdog_extension_count,
        "scenario_event_applied": event_applied, "runtime_controls": controls,
        "operator_review_status": operator_review_status,
        "launch_schedule": launch_schedule,
        "route_traffic_admission": {
            "status": "APPLIED" if headway_supported else "NOT_SUPPORTED_BY_ADAPTER",
            "physical_concurrent_route_capacity": None,
            "capacity_source": "NOT_MULTI_TRUCK_VALIDATED",
            "runtime_admission_strategy": (
                "TOPOLOGY_CONFLICT_SERIALIZED_WITH_HEADWAY"
            ),
            "topology_route_count": len(task_route_edges),
            "final_state": route_admission_state if headway_supported else {},
            "boundary": (
                "P6 remains isolated-route evidence. Runtime uses staged "
                "headway, topology conflict serialization and BasicAgent "
                "obstacle avoidance; this is not a "
                "multi-truck collision-safety proof."
            ),
        },
        "runtime_traffic_coordination": _runtime_traffic_summary(
            traffic_coordination_state,
            supported=traffic_coordination_supported,
        ),
        "runtime_task_recovery": {
            "schema_version": "openpit.runtime-task-recovery.v1",
            "status": (
                "APPLIED" if runtime_recovery_attempts else "NOT_REQUIRED"
            ),
            "maximum_reassignments_per_task": (
                MAX_RUNTIME_TASK_REASSIGNMENTS
            ),
            "reassignment_count": sum(runtime_recovery_attempts.values()),
            "attempts_by_task": dict(sorted(
                runtime_recovery_attempts.items()
            )),
            "policy": "HARD_CONSTRAINTS_THEN_RUNTIME_LOAD",
        },
        "headway_control_supported": headway_supported,
        "physical_speed_caps": physical_speed_evidence,
        "route_evidence_admission": route_evidence_admission,
        "runtime_route_sequence_count": len(runtime_route_waypoints),
        "physical_speed_cap_supported": speed_cap_supported if not check_only else None,
        "assignments": structural.get("assignments", []),
        "tasks": [item.to_dict() for item in tasks],
        "final_vehicle_states": final_states, "events": events,
        "execution_diagnostics": _task_execution_diagnostics(tasks, controls),
        "runtime_telemetry": runtime_telemetry,
        "event_count": len(events), "execution_commands": [command.to_dict()],
        "execution_feedback": feedback, "destroyed_vehicle_count": destroyed,
        "database_recording": False,
        "simulation_claim": "carla_basic_agent_multi_vehicle_production_cycle_and_event_execution",
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
    result["execution_failure_summary"] = _execution_failure_summary(
        result["execution_diagnostics"]
    )
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
    normalized = normalize_scenario_run_result(
        result, scenario_key=name, scenario_spec=common_spec
    )
    normalized["production_runtime"] = _carla_production_runtime(
        tasks, events, production_configuration
    )
    normalized["data_contract"]["production_runtime"] = (
        "openpit.production-runtime.v1"
    )
    normalized["concrete_episode_v2"]["production_system"][
        "runtime_status"
    ] = normalized["production_runtime"]["mode"]
    normalized["concrete_episode_v2"]["execution_binding"][
        "production_state_machine"
    ] = (
        "COMMON_CARLA_RUNTIME_V1" if production_configuration.get("status")
        == "CONFIGURED" else "LEGACY_POINT_TO_POINT_ADAPTER"
    )
    return normalized


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
