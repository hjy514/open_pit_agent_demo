"""Command-line entry point for Mock and CARLA feasibility runs."""

import argparse
import json
import os
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from .adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from .adapters.mock_adapter import MockAdapter
from .config import ConfigError, ScenarioConfig, load_config
from .decision_intelligence import (
    build_assignment_decisions,
    build_risk_guidance,
    finalize_learning_and_acceptance,
    load_imitation_memory,
)
from .evidence import EvidenceRecorder
from .interface_exporter import export_interface
from .models import Task, VehicleState
from .monitoring import (
    MonitoringConfigError,
    MonitoringLayout,
    build_fixed_observations,
    build_mobile_observations,
    load_monitoring_layout,
    monitoring_summary,
)
from .risk import (
    RiskConfigError,
    RiskScenario,
    RuleBasedRiskEngine,
    actions_for_assessment,
    create_post_action_feedback_observation,
    create_risk_task,
    load_risk_scenario,
    observations_at_tick,
)
from .restrictions import RestrictionRegistry
from .scenario_runtime import ResolvedScenario, resolve_scenario
from .scenario import build_episode
from .scheduler import (
    BaselineScheduler,
    SchedulingError,
    release_failed_vehicle_tasks,
    release_hazard_affected_tasks,
    tasks_from_zones,
)
RUNTIME_API_URL = os.environ.get(
    "OPENPIT_RUNTIME_API_URL",
    "http://127.0.0.1:8000/runtime/sync",
)
RUNTIME_API_BASE_URL = RUNTIME_API_URL.rsplit(
    "/runtime/sync", 1
)[0]

SLOPE_STATE_LABELS = {
    "stable": "稳定",
    "rainfall_infiltration": "降雨入渗",
    "progressive_deformation": "渐进变形",
    "accelerating_deformation": "加速变形",
    "pre_failure": "临滑预警",
    "failure": "局部失稳",
    "post_failure_monitoring": "滑后监测",
}

from .work_order import (
    WorkOrderError,
    WorkOrderManager,
    WorkOrderTransition,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "town03.json"
DEFAULT_ARTIFACTS = PROJECT_ROOT / "artifacts" / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Town03 ordinary-vehicle feasibility demo"
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "carla-check", "carla-run"),
        default="mock",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument(
        "--risk-config",
        type=Path,
        help="Synthetic risk observations and transparent rule thresholds",
    )
    parser.add_argument(
        "--monitoring-config",
        type=Path,
        help="Fixed monitoring areas, stations and raw observations",
    )
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="Inject the configured vehicle failure and reschedule its tasks",
    )
    parser.add_argument(
        "--load-map",
        action="store_true",
        help="Load the configured CARLA map before connecting",
    )
    parser.add_argument(
        "--spawn-missing",
        action="store_true",
        help="Spawn configured vehicles whose role_name is absent",
    )
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument(
        "--realtime-delay",
        type=float,
        default=0.0,
        help="Mock模式各阶段之间的演示等待秒数，0表示不等待",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Override the configured reproducible scenario seed",
    )
    parser.add_argument(
        "--randomize",
        action="store_true",
        help="Generate and record a new scenario seed for this run",
    )
    return parser


def _states_payload(states: List[VehicleState]) -> List[Dict[str, object]]:
    return [state.to_dict() for state in states]


def _tasks_payload(tasks: List[Task]) -> List[Dict[str, object]]:
    return [task.to_dict() for task in tasks]


def _zones_payload(zones) -> List[Dict[str, object]]:
    return [
        {
            "zone_id": zone.zone_id,
            "display_name": zone.display_name,
            "priority": zone.priority,
            "required_capabilities": list(
                zone.required_capabilities
            ),
            "target_spawn_point_index": (
                zone.target_spawn_point_index
            ),
            "position": {
                "x": zone.mock_position.x,
                "y": zone.mock_position.y,
                "z": zone.mock_position.z,
            },
            "initial_task": zone.initial_task,
        }
        for zone in zones
    ]


def _print_json(label: str, payload) -> None:
    print("\n{}".format(label))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _record_adapter_events(
    adapter: CarlaAdapter, recorder: EvidenceRecorder
) -> List[Dict[str, object]]:
    events = adapter.drain_events()
    for event in events:
        payload = dict(event["payload"])
        payload["tick"] = event["tick"]
        recorder.record(str(event["event_type"]), payload)
    return events


def _record_work_order_transitions(
    transitions: List[WorkOrderTransition],
    recorder: EvidenceRecorder,
) -> None:
    for transition in transitions:
        recorder.record(
            "work_order_status_changed", transition.to_dict()
        )


def _record_assignment_decisions(
    assignments,
    context: str,
    tick: Optional[int],
    resolved_scenario: ResolvedScenario,
    recorder: EvidenceRecorder,
    decision_records: List[Dict[str, object]],
) -> None:
    created = build_assignment_decisions(
        assignments,
        context=context,
        tick=tick,
        scenario_seed=resolved_scenario.seed,
    )
    decision_records.extend(created)
    for item in created:
        recorder.record("agent_decision", item)


def _run_result(tasks: List[Task]) -> Dict[str, object]:
    counts = Counter(task.status for task in tasks)
    total = len(tasks)
    completed = counts["completed"]
    if total and completed == total:
        status = "PASS"
    elif counts["timed_out"] > 0:
        status = "FAIL"
    else:
        status = "PARTIAL"
    return {
        "status": status,
        "task_status_counts": dict(sorted(counts.items())),
        "completion_rate": round(completed / float(total), 4) if total else 0.0,
        "all_tasks_terminal": all(
            task.status in {"completed", "timed_out", "cancelled"}
            for task in tasks
        ),
    }

def sync_runtime_state(
    states,
    tasks,
    assignments=None,
    event=None,
    run_id=None,
    risk=None,
    decision=None,
    environment=None,
    monitoring=None,
):
    """
    通过HTTP把当前运行状态推送到独立API进程。

    注意:
    run_demo.py和uvicorn运行在两个不同进程中，
    不能依靠Python模块单例共享内存。
    """

    payload = {
        "vehicles": [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (states or [])
        ],
        "tasks": [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (tasks or [])
        ],
        "assignments": [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (assignments or [])
        ],
    }

    if event is not None:
        payload["event"] = event

    if run_id is not None:
        payload["run_id"] = run_id

    if risk is not None:
        payload["risk"] = risk

    if decision is not None:
        payload["decision"] = decision

    if monitoring is not None:
        payload["monitoring"] = monitoring
        mirrored_environment = dict(environment or {})
        mirrored_environment["monitoring_runtime"] = dict(
            monitoring
        )
        payload["environment"] = mirrored_environment
    elif environment is not None:
        payload["environment"] = environment

    request_body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    http_request = urllib_request.Request(
        RUNTIME_API_URL,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(
            http_request,
            timeout=1.5,
        ) as response:
            response.read()
    except (
        urllib_error.URLError,
        urllib_error.HTTPError,
        TimeoutError,
        OSError,
    ) as exc:
        print(
            "WARNING: Runtime状态同步失败，"
            "不影响当前Demo继续运行：{}".format(exc)
        )



def _runtime_json_request(
    method: str,
    path: str,
    payload=None,
    timeout: float = 1.0,
):
    url = "{}{}".format(RUNTIME_API_BASE_URL, path)
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib_request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    with urllib_request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def fetch_pending_runtime_commands():
    try:
        data = _runtime_json_request(
            "GET",
            "/commands/pending?limit=20",
            timeout=0.8,
        )
    except (
        urllib_error.URLError,
        urllib_error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
    ):
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def acknowledge_runtime_command(
    command_id: str,
    status: str,
    message: str,
    result=None,
) -> None:
    try:
        _runtime_json_request(
            "POST",
            "/commands/{}/ack".format(command_id),
            payload={
                "status": status,
                "message": message,
                "result": result,
            },
            timeout=1.0,
        )
    except (
        urllib_error.URLError,
        urllib_error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        print(
            "WARNING: 命令回执上传失败 {}：{}".format(
                command_id, exc
            )
        )


def execute_runtime_command(
    adapter: CarlaAdapter,
    command,
):
    action = str(command.get("action", ""))
    vehicle_id = command.get("vehicle_id")

    if action == "pause_vehicle":
        return adapter.pause_vehicle(str(vehicle_id))
    if action == "resume_vehicle":
        return adapter.resume_vehicle(str(vehicle_id))
    if action == "emergency_stop":
        return adapter.emergency_stop_vehicle(str(vehicle_id))
    if action == "manual_dispatch":
        task_id = command.get("task_id")
        if not task_id:
            raise CarlaAdapterError("manual_dispatch缺少task_id")
        return adapter.reassign_task(
            str(task_id),
            str(vehicle_id),
            speed_limit_kmh=command.get("speed_limit_kmh"),
        )

    raise CarlaAdapterError(
        "CARLA运行进程不支持命令：{}".format(action)
    )


def process_runtime_commands(
    adapter: CarlaAdapter,
    recorder: EvidenceRecorder,
    tick_index: int,
):
    processed = []
    for command in fetch_pending_runtime_commands():
        command_id = str(command.get("command_id", ""))
        if not command_id:
            continue

        recorder.record(
            "runtime_command_received",
            {
                "tick": tick_index,
                "command": command,
            },
        )
        try:
            result = execute_runtime_command(adapter, command)
            message = "CARLA执行成功"
            acknowledge_runtime_command(
                command_id,
                "succeeded",
                message,
                result=result,
            )
            recorder.record(
                "runtime_command_succeeded",
                {
                    "tick": tick_index,
                    "command": command,
                    "result": result,
                },
            )
            processed.append(
                {
                    "command": command,
                    "status": "succeeded",
                    "result": result,
                }
            )
        except Exception as exc:
            message = str(exc)
            acknowledge_runtime_command(
                command_id,
                "failed",
                message,
                result={"error": message},
            )
            recorder.record(
                "runtime_command_failed",
                {
                    "tick": tick_index,
                    "command": command,
                    "error": message,
                },
            )
            processed.append(
                {
                    "command": command,
                    "status": "failed",
                    "error": message,
                }
            )
    return processed


def _realtime_wait(seconds: float, stage: str) -> None:
    """在Mock演示阶段之间增加可观察等待。"""
    if seconds <= 0:
        return

    print(
        "\n[Realtime Demo] {}，等待 {:.1f} 秒……".format(
            stage,
            seconds,
        )
    )
    time.sleep(seconds)


def _record_fixed_monitoring(
    recorder: EvidenceRecorder,
    layout: Optional[MonitoringLayout],
    risk_scenario: Optional[RiskScenario],
) -> Dict[str, object]:
    if layout is None:
        return {}

    observations = build_fixed_observations(
        layout,
        (
            risk_scenario.observations
            if risk_scenario is not None
            else None
        ),
    )
    summary = monitoring_summary(layout, observations)
    recorder.write_jsonl(
        "monitoring_observations.jsonl",
        [item.to_dict() for item in observations],
    )
    recorder.record(
        "monitoring_layout_loaded",
        {
            "layout_id": layout.layout_id,
            "area_count": len(layout.areas),
            "fixed_station_count": len(layout.stations),
            "synthetic_data": layout.synthetic_data,
            "dataset_label": layout.dataset_label,
        },
    )
    for observation in observations:
        recorder.record(
            "fixed_monitoring_observation",
            observation.to_dict(),
        )
    return summary


def _record_mobile_monitoring(
    recorder: EvidenceRecorder,
    layout: Optional[MonitoringLayout],
    states: List[VehicleState],
    tick: int,
    risk_observation=None,
    telemetry_source: str = "carla_runtime",
) -> int:
    if layout is None:
        return 0
    observations = build_mobile_observations(
        layout,
        states,
        tick,
        risk_observation=risk_observation,
        telemetry_source=telemetry_source,
    )
    recorder.append_jsonl(
        "monitoring_observations.jsonl",
        [item.to_dict() for item in observations],
    )
    for observation in observations:
        recorder.record(
            "mobile_monitoring_observation",
            observation.to_dict(),
        )
    return len(observations)


def _runtime_monitoring_payload(
    monitoring_data: Dict[str, object],
    mobile_observation_count: int,
    phase: str,
    phase_index: int,
    work_orders: WorkOrderManager,
    restrictions: RestrictionRegistry,
    tasks=(),
    feedback_count: int = 0,
    assessment=None,
    latest_event: str = "",
) -> Dict[str, object]:
    work_order_summary = work_orders.summary()
    restriction_summary = restrictions.summary()
    risk_payload = (
        assessment.to_dict()
        if hasattr(assessment, "to_dict")
        else (assessment if isinstance(assessment, dict) else {})
    )
    active_restrictions = int(
        restriction_summary.get("active_road_restriction_count", 0)
    )
    all_closed = work_orders.all_closed()
    handover_tasks = [
        item for item in (tasks or [])
        if getattr(item, "handover_reason", None)
    ]
    takeover_task = handover_tasks[0] if handover_tasks else None
    takeover_status = "未触发"
    if takeover_task is not None:
        if takeover_task.status == "completed":
            takeover_status = "接管任务已完成"
        elif takeover_task.status == "executing":
            takeover_status = "接管车辆执行中"
        elif takeover_task.recommended_vehicle_id:
            takeover_status = "等待调度员确认"
        else:
            takeover_status = "AI正在评估候选车辆"
    if active_restrictions:
        road_control_status = "风险区准入管控中"
        route_safety_status = (
            "危险路段已封控，安全中间点绕行已生效"
            if restriction_summary.get("route_avoidance_enforced")
            else "已启用策略级风险区规避"
        )
    elif all_closed and feedback_count:
        road_control_status = "已解除"
        route_safety_status = "复核安全，CARLA导航路线已恢复"
    elif str(risk_payload.get("level", "")).lower() in {
        "orange",
        "red",
    }:
        road_control_status = "无需区域封锁，边坡异常持续标记"
        route_safety_status = (
            "候选车辆从安全侧接管，后续车辆持续接收风险提示"
        )
    else:
        road_control_status = "未启动"
        route_safety_status = "CARLA导航路线已生成"
    return {
        "phase": phase,
        "phase_index": int(phase_index),
        "fixed_station_count": int(
            monitoring_data.get("fixed_station_count", 0)
        ),
        "mobile_equipment_count": int(
            monitoring_data.get("mobile_equipment_count", 0)
        ),
        "fixed_observation_count": int(
            monitoring_data.get("fixed_observation_count", 0)
        ),
        "mobile_observation_count": int(mobile_observation_count),
        "total_observation_count": int(
            monitoring_data.get("fixed_observation_count", 0)
        )
        + int(mobile_observation_count),
        "risk_level": risk_payload.get("level", "UNKNOWN"),
        "previous_risk_level": risk_payload.get(
            "previous_level", "UNKNOWN"
        ),
        "risk_trend": risk_payload.get("trend", "stable"),
        "risk_zone": risk_payload.get("zone_id", "-"),
        "slope_state": risk_payload.get("slope_state", "stable"),
        "slope_state_label": SLOPE_STATE_LABELS.get(
            risk_payload.get("slope_state", "stable"), "未知"
        ),
        "work_order_count": int(
            work_order_summary.get("work_order_count", 0)
        ),
        "closed_work_order_count": sum(
            item.get("status") == "closed"
            for item in work_order_summary.get("work_orders", [])
        ),
        "feedback_count": int(feedback_count),
        "takeover_status": takeover_status,
        "takeover_task_count": len(handover_tasks),
        "takeover_recommended_vehicle_id": (
            takeover_task.recommended_vehicle_id
            if takeover_task is not None
            else None
        ),
        "takeover_selected_vehicle_id": (
            takeover_task.assigned_vehicle_id
            if takeover_task is not None
            else None
        ),
        "takeover_candidate_count": (
            len(takeover_task.candidate_evaluations)
            if takeover_task is not None
            else 0
        ),
        "road_control_status": road_control_status,
        "route_safety_status": route_safety_status,
        "route_avoidance_enforced": bool(
            restriction_summary.get("route_avoidance_enforced", False)
        ),
        "hazard_information_active": str(
            risk_payload.get("level", "")
        ).lower() in {"orange", "red"},
        "closed_loop_complete": bool(
            (feedback_count and all_closed)
            or phase == "闭环完成"
        ),
        "latest_event": latest_event,
        "data_label": monitoring_data.get(
            "monitoring_dataset_label", ""
        ),
    }


def _record_slope_state_transition(
    recorder: EvidenceRecorder,
    assessment,
    previous_slope_state: Optional[str],
) -> str:
    """Emit an auditable physical-slope state transition."""

    current = str(getattr(assessment, "slope_state", "stable"))
    if current != previous_slope_state:
        recorder.record(
            "slope_state_changed",
            {
                "tick": int(assessment.tick),
                "risk_level": assessment.level,
                "previous_slope_state": previous_slope_state,
                "slope_state": current,
                "slope_state_label": SLOPE_STATE_LABELS.get(
                    current, "未知"
                ),
                "zone_id": assessment.zone_id,
                "reason": "synthetic_slope_event_stage_transition",
            },
        )
    return current


def _record_risk_level_transition(
    recorder: EvidenceRecorder, assessment
) -> None:
    """Record level changes separately from every raw risk assessment."""

    if assessment.level == assessment.previous_level:
        return
    recorder.record(
        "risk_level_changed",
        {
            "tick": int(assessment.tick),
            "previous_level": assessment.previous_level,
            "risk_level": assessment.level,
            "slope_state": getattr(assessment, "slope_state", "stable"),
            "zone_id": assessment.zone_id,
            "reasons": list(getattr(assessment, "reasons", [])),
        },
    )


def _activate_configured_safe_route(
    adapter,
    config,
    restrictions: RestrictionRegistry,
    restriction,
    recorder,
    tick: int,
) -> Optional[Dict[str, object]]:
    """Activate the configured CARLA emergency bypass once per run."""

    if restrictions.summary().get("route_avoidance_enforced"):
        return None
    emergency = config.scenario_variables.get("emergency_event", {})
    disaster = config.scenario_variables.get("disaster", {})
    safe_route = (
        emergency.get("safe_route", {})
        if isinstance(emergency, dict)
        else {}
    )
    configure = getattr(adapter, "configure_safe_route", None)
    if not isinstance(safe_route, dict) or not callable(configure):
        return None
    route_plan_id = str(safe_route.get("route_plan_id", "")).strip()
    task_types = safe_route.get("task_types", [])
    waypoint_indices = safe_route.get(
        "waypoint_spawn_point_indices",
        [safe_route.get("waypoint_spawn_point_index")],
    )
    if not route_plan_id or not isinstance(task_types, list):
        return None
    if (
        not isinstance(waypoint_indices, list)
        or not waypoint_indices
        or any(index is None for index in waypoint_indices)
    ):
        return None
    strategy = str(
        safe_route.get(
            "strategy", "carla_basic_agent_via_safe_waypoint"
        )
    )
    if safe_route.get("execution_mode") == "logical_plan_only":
        payload = {
            "route_plan_id": route_plan_id,
            "waypoint_spawn_point_indices": [
                int(index) for index in waypoint_indices
            ],
            "task_types": [str(value) for value in task_types],
            "blocked_road_segment_id": restriction.road_segment_id,
            "strategy": strategy,
            "execution_mode": "logical_plan_only",
            "replanned_task_ids": [],
        }
        restrictions.mark_route_avoidance_enforced(route_plan_id, strategy)
        recorder.record("hazard_route_replanned", {"tick": tick, **payload})
        return payload
    payload = configure(
        route_plan_id=route_plan_id,
        waypoint_spawn_point_indices=[
            int(index) for index in waypoint_indices
        ],
        task_types=[str(value) for value in task_types],
        blocked_road_segment_id=restriction.road_segment_id,
    )
    restrictions.mark_route_avoidance_enforced(
        route_plan_id,
        str(
            safe_route.get(
                "strategy", "carla_basic_agent_via_safe_waypoint"
            )
        ),
    )
    event_payload = {
        "tick": int(tick),
        "event_id": emergency.get("event_id") or disaster.get("event_id"),
        "event_name": emergency.get("event_name") or disaster.get("event_name"),
        "trigger_evidence": emergency.get("trigger_evidence", []),
        "blocked_road_segment_id": restriction.road_segment_id,
        **payload,
    }
    recorder.record("safe_route_replanned", event_payload)
    return event_payload


def _place_affected_vehicle_in_safe_hold(
    adapter,
    config: ScenarioConfig,
    assessment,
    recorder: EvidenceRecorder,
    tick: int,
) -> Optional[Dict[str, object]]:
    """Apply and record the affected truck's immediate protective action."""

    slope_event = config.scenario_variables.get("slope_event", {})
    if not isinstance(slope_event, dict):
        return None
    vehicle_id = str(slope_event.get("affected_vehicle_id", "")).strip()
    stop_vehicle = getattr(adapter, "emergency_stop_vehicle", None)
    if not vehicle_id or not callable(stop_vehicle):
        return None
    recorder.record(
        "affected_vehicle_detected",
        {
            "tick": int(tick),
            "vehicle_id": vehicle_id,
            "risk_level": assessment.level,
            "slope_state": getattr(assessment, "slope_state", "failure"),
            "action": "protective_safe_hold_before_evacuation",
        },
    )
    result = stop_vehicle(vehicle_id)
    recorder.record(
        "hazard_vehicle_safe_hold",
        {
            "tick": int(tick),
            "vehicle_id": vehicle_id,
            "status": result.get("status", "emergency_stop"),
            "task_id": result.get("task_id"),
            "reason": "east_slope_haul_road_r1_red_risk",
        },
    )
    return result


def _process_closed_loop_feedback(
    work_orders: WorkOrderManager,
    risk_engine: RuleBasedRiskEngine,
    risk_scenario: RiskScenario,
    recorder: EvidenceRecorder,
    tick: int,
):
    transitions = []
    records = []
    # Feedback is an independent post-action recheck.  It must not mutate the
    # live engine's per-zone history, otherwise a later persistent red sample
    # is incorrectly treated as a brand-new escalation.
    feedback_engine = RuleBasedRiskEngine(risk_scenario)
    for baseline_observation in risk_scenario.observations:
        feedback_engine.assess(baseline_observation)
    for order in work_orders.ready_for_feedback(tick):
        feedback_id = "feedback-{}-{:06d}".format(
            order.work_order_id, tick
        )
        observation = create_post_action_feedback_observation(
            risk_scenario,
            tick=tick,
            feedback_id=feedback_id,
            # The recheck must be evaluated against the original hazard
            # zone; the response task itself may be dispatched to H1.
            zone_id=risk_scenario.observations[-1].zone_id,
        )
        assessment = feedback_engine.assess(observation)
        transition = work_orders.review_with_feedback(
            order.task_id,
            tick=tick,
            feedback_level=assessment.level,
            feedback_id=feedback_id,
        )
        decision = (
            "close_work_order"
            if transition.to_status == "closed"
            else "escalate_work_order"
        )
        record = {
            "feedback_id": feedback_id,
            "work_order_id": order.work_order_id,
            "task_id": order.task_id,
            "assigned_vehicle_id": order.assigned_vehicle_id,
            "input_source": "synthetic_post_action_recheck",
            "observation": observation.to_dict(),
            "assessment": assessment.to_dict(),
            "decision": decision,
            "resulting_work_order_status": transition.to_status,
        }
        recorder.record(
            "closed_loop_feedback_observation",
            record,
        )
        recorder.record(
            "closed_loop_feedback_decision",
            {
                "feedback_id": feedback_id,
                "work_order_id": order.work_order_id,
                "risk_level": assessment.level,
                "decision": decision,
                "resulting_work_order_status": transition.to_status,
            },
        )
        recorder.append_jsonl(
            "feedback_observations.jsonl", [record]
        )
        transitions.append(transition)
        records.append(record)
    return transitions, records


def run_mock(
    config: ScenarioConfig,
    recorder: EvidenceRecorder,
    inject: bool,
    risk_scenario: Optional[RiskScenario],
    monitoring_layout: Optional[MonitoringLayout],
    resolved_scenario: ResolvedScenario,
    realtime_delay: float,
) -> None:
    adapter = MockAdapter(config)
    imitation_preferences, imitation_memory = (
        load_imitation_memory(
            recorder.run_dir.parent, config.scenario_id
        )
    )
    scheduler = BaselineScheduler(
        experience_preferences=imitation_preferences
    )
    work_orders = WorkOrderManager()
    restrictions = RestrictionRegistry()
    tasks = tasks_from_zones(config.zones)
    decision_records = []
    risk_guidance = []
    adapter.connect()
    try:
        monitoring_data = _record_fixed_monitoring(
            recorder,
            monitoring_layout,
            risk_scenario,
        )
        mobile_observation_count = 0
        mobile_equipment_ids = set()
        states = list(adapter.list_states())
        recorder.record(
            "imitation_memory_loaded", imitation_memory
        )
        assignments = scheduler.assign(tasks, states, config.zones)
        adapter.dispatch(tasks, config.zones)
        sync_runtime_state(
            states,
            tasks,
            assignments,
            {
                "type": "scheduler",
                "message": "初始任务调度完成",
            },
            run_id=recorder.run_id,
        )
        _realtime_wait(
            realtime_delay,
            "初始调度状态已推送到UI",
        )
        recorder.record("instruction_received", {"text": config.instruction})
        recorder.record(
            "initial_vehicle_states", {"vehicles": _states_payload(states)}
        )
        recorder.record(
            "initial_schedule",
            {
                "assignments": [item.to_dict() for item in assignments],
                "tasks": _tasks_payload(tasks),
            },
        )
        _record_assignment_decisions(
            assignments,
            "initial_schedule",
            0,
            resolved_scenario,
            recorder,
            decision_records,
        )
        recorder.record(
            "runtime_scene",
            {
                "map_name": "mock",
                "zones": _zones_payload(config.zones),
            },
        )
        _print_json("初始调度", [item.to_dict() for item in assignments])

        risk_assessments = []
        risk_task_ids = []
        previous_slope_state = None
        if risk_scenario is not None:
            engine = RuleBasedRiskEngine(risk_scenario)
            recorder.record(
                "risk_dataset_loaded",
                {
                    "risk_scenario_id": risk_scenario.scenario_id,
                    "dataset_label": risk_scenario.dataset_label,
                    "synthetic_data": risk_scenario.synthetic_data,
                    "model_version": risk_scenario.model_version,
                },
            )
            for observation in risk_scenario.observations:
                mobile_states = list(adapter.list_states())
                recorded_count = _record_mobile_monitoring(
                    recorder,
                    monitoring_layout,
                    mobile_states,
                    observation.tick,
                    risk_observation=observation,
                    telemetry_source="mock_adapter",
                )
                mobile_observation_count += recorded_count
                if recorded_count:
                    mobile_equipment_ids.update(
                        item.vehicle_id for item in mobile_states
                    )
                recorder.record("risk_observation", observation.to_dict())
                assessment = engine.assess(observation)
                risk_assessments.append(assessment)
                recorder.record("risk_assessed", assessment.to_dict())
                previous_slope_state = _record_slope_state_transition(
                    recorder, assessment, previous_slope_state
                )
                _record_risk_level_transition(recorder, assessment)
                guidance = build_risk_guidance(
                    assessment, risk_scenario, resolved_scenario
                )
                risk_guidance.append(guidance)
                recorder.record(
                    "risk_guidance_generated", guidance
                )
                restriction_action = risk_scenario.restriction
                if (
                    restriction_action is not None
                    and assessment.level
                    in restriction_action.trigger_levels
                    and assessment.level != assessment.previous_level
                ):
                    restriction = restrictions.activate(
                        assessment,
                        restriction_action,
                        tick=observation.tick,
                    )
                    cancelled_task_ids = (
                        restrictions.apply_to_tasks(tasks)
                    )
                    recorder.record(
                        "road_restriction_activated",
                        {
                            "restriction": restriction.to_dict(),
                            "cancelled_task_ids": cancelled_task_ids,
                        },
                    )
                created = []
                for action in actions_for_assessment(
                    risk_scenario, assessment
                ):
                    task = create_risk_task(assessment, action)
                    order = work_orders.create_from_risk(
                        assessment,
                        task,
                        action,
                        tick=observation.tick,
                    )
                    recorder.record(
                        "work_order_created", order.to_dict()
                    )
                    tasks.append(task)
                    created.append((task, action))
                if not created:
                    continue
                states = list(adapter.list_states())
                risk_assignments = scheduler.assign(
                    tasks, states, config.zones
                )
                sync_runtime_state(
                    states,
                    tasks,
                    risk_assignments,
                    {
                        "type":"risk_response",
                        "message":
                        "风险触发，重新生成任务并调度"
                    },
                    run_id=recorder.run_id,
                    risk=assessment.to_dict(),
                )
                _realtime_wait(
                    realtime_delay,
                    "风险响应状态已推送到UI",
                )
                assignment_by_task = {
                    item.task_id: item
                    for item in risk_assignments
                }
                for task, _ in created:
                    risk_assignment = assignment_by_task[
                        task.task_id
                    ]
                    transition = work_orders.assign(
                        task.task_id,
                        risk_assignment.vehicle_id,
                        tick=observation.tick,
                    )
                    _record_work_order_transitions(
                        [transition], recorder
                    )
                    risk_task_ids.append(task.task_id)
                    recorder.record(
                        "risk_task_created",
                        {
                            "assessment": assessment.to_dict(),
                            "task": task.to_dict(),
                            "assignment": (
                                risk_assignment.to_dict()
                            ),
                        },
                    )
                _record_assignment_decisions(
                    [
                        assignment_by_task[task.task_id]
                        for task, _ in created
                    ],
                    "risk_response",
                    observation.tick,
                    resolved_scenario,
                    recorder,
                    decision_records,
                )
                adapter.dispatch(tasks, config.zones)
        elif monitoring_layout is not None:
            mobile_states = list(adapter.list_states())
            mobile_observation_count += _record_mobile_monitoring(
                recorder,
                monitoring_layout,
                mobile_states,
                0,
                telemetry_source="mock_adapter",
            )
            mobile_equipment_ids.update(
                item.vehicle_id for item in mobile_states
            )

        released = []
        reassigned = []
        if inject:
            failure_plan = resolved_scenario.failure_plan(config)
            if failure_plan is None:
                raise ConfigError("failure injection requested without a failure plan")
            failed, failure_tick = failure_plan
            adapter.inject_fault(failed)
            released = release_failed_vehicle_tasks(tasks, failed)
            states = list(adapter.list_states())

            recorded_count = _record_mobile_monitoring(
                recorder,
                monitoring_layout,
                states,
                failure_tick,
                telemetry_source="mock_adapter",
            )
            mobile_observation_count += recorded_count
            if recorded_count:
                mobile_equipment_ids.update(
                    item.vehicle_id for item in states
                )

            sync_runtime_state(
                states,
                tasks,
                event={
                    "type": "vehicle_fault",
                    "message": "{}发生故障，相关任务已释放".format(
                        failed
                    ),
                },
                run_id=recorder.run_id,
            )
            _realtime_wait(
                realtime_delay,
                "故障状态已推送到UI",
            )

            reassigned = scheduler.assign(
                tasks, states, config.zones, excluded_vehicle_ids={failed}
            )
            sync_runtime_state(
                states,
                tasks,
                reassigned,
                {
                    "type": "fault_recovery",
                    "message": "{}故障，任务重新分配".format(
                        failed
                    ),
                },
                run_id=recorder.run_id,
            )
            _realtime_wait(
                realtime_delay,
                "故障恢复调度已推送到UI",
            )
            adapter.dispatch(tasks, config.zones)
            recorder.record(
                "vehicle_fault",
                {
                    "tick": failure_tick,
                    "vehicle_id": failed,
                    "released_task_ids": released,
                    "scenario_seed": resolved_scenario.seed,
                },
            )
            recorder.record(
                "reschedule",
                {
                    "assignments": [item.to_dict() for item in reassigned],
                    "tasks": _tasks_payload(tasks),
                },
            )
            _record_assignment_decisions(
                reassigned,
                "failure_recovery",
                failure_tick,
                resolved_scenario,
                recorder,
                decision_records,
            )
            _print_json("故障后重新调度", [item.to_dict() for item in reassigned])

        final_states = list(adapter.list_states())
        if monitoring_data:
            monitoring_data["mobile_equipment_count"] = len(
                mobile_equipment_ids
            )
            monitoring_data["mobile_observation_count"] = (
                mobile_observation_count
            )
        summary = {
            "mode": "mock",
            "scenario_id": config.scenario_id,
            "run_id": recorder.run_id,
            "scenario_seed": resolved_scenario.seed,
            "scenario_mode": resolved_scenario.mode,
            "imitation_memory": imitation_memory,
            "initial_assignment_count": len(assignments),
            "failure_injected": inject,
            "released_task_ids": released,
            "reassignment_count": len(reassigned),
            "risk_scenario_id": (
                risk_scenario.scenario_id
                if risk_scenario is not None
                else None
            ),
            "risk_assessments": [
                item.to_dict() for item in risk_assessments
            ],
            "risk_guidance": risk_guidance,
            "risk_task_ids": risk_task_ids,
            "risk_triggered": bool(risk_task_ids),
            "tasks": _tasks_payload(tasks),
            "zones": _zones_payload(config.zones),
            "vehicle_states": _states_payload(final_states),
            "status": "PASS",
            **monitoring_data,
            **(
                {
                    **work_orders.summary(),
                    **restrictions.summary(),
                }
                if risk_scenario is not None
                else {}
            ),
        }
        sync_runtime_state(
            final_states,
            tasks,
            event={
                "type": "run_completed",
                "message": "Mock演示运行结束",
            },
            run_id=recorder.run_id,
            decision={"status": summary.get("status", "UNKNOWN")},
        )
        _realtime_wait(
            realtime_delay,
            "运行结束状态已推送到UI",
        )
        finalize_learning_and_acceptance(
            recorder, summary, decision_records
        )
        recorder.record("run_completed", summary)
        recorder.write_json("summary.json", summary)
        export_interface(summary, recorder)
        print("\nMock演示完成：{}".format(recorder.run_dir))
    finally:
        adapter.close()


def run_carla(
    config: ScenarioConfig,
    recorder: EvidenceRecorder,
    mode: str,
    load_map: bool,
    spawn_missing: bool,
    ticks: int,
    inject: bool,
    risk_scenario: Optional[RiskScenario],
    monitoring_layout: Optional[MonitoringLayout],
    resolved_scenario: ResolvedScenario,
) -> None:
    adapter = CarlaAdapter(config, load_map=load_map)
    imitation_preferences, imitation_memory = (
        load_imitation_memory(
            recorder.run_dir.parent, config.scenario_id
        )
    )
    scheduler = BaselineScheduler(
        experience_preferences=imitation_preferences
    )
    work_orders = WorkOrderManager()
    restrictions = RestrictionRegistry()
    tasks = tasks_from_zones(config.zones)
    risk_engine = (
        RuleBasedRiskEngine(risk_scenario)
        if risk_scenario is not None
        else None
    )
    risk_assessments = []
    risk_task_ids = []
    hazard_takeover_triggered = False
    previous_slope_state = None
    risk_guidance = []
    decision_records = []
    closed_loop_feedback_records = []
    failure_plan = resolved_scenario.failure_plan(config)
    failure_vehicle_id, failure_tick = failure_plan or (None, None)
    last_risk_tick = (
        max(item.tick for item in risk_scenario.observations)
        if risk_scenario is not None
        else None
    )
    adapter.connect()
    try:
        monitoring_data = _record_fixed_monitoring(
            recorder,
            monitoring_layout,
            risk_scenario,
        )
        mobile_observation_count = 0
        mobile_equipment_ids = set()
        adapter.ensure_vehicles(spawn_missing=spawn_missing)
        states = list(adapter.list_states())
        if monitoring_data:
            monitoring_data["mobile_equipment_count"] = len(states)
        recorder.record(
            "imitation_memory_loaded", imitation_memory
        )
        recorder.record(
            "instruction_received",
            {
                "text": config.instruction,
                "source": "allowlisted_scenario",
            },
        )
        recorder.record(
            "agent_instruction_parsed",
            {
                "intent": (
                    resolved_scenario.mission.get(
                        "mission_type",
                        "configured_mission",
                    )
                ),
                "natural_language_instruction": (
                    resolved_scenario.mission.get(
                        "natural_language_instruction",
                        config.instruction,
                    )
                ),
                "success_criteria": list(
                    resolved_scenario.mission.get(
                        "success_criteria", []
                    )
                ),
                "structured_output_source": (
                    "scenario_v2_allowlisted_baseline"
                ),
                "large_language_model_used": False,
            },
        )
        recorder.record(
            "carla_connected",
            {
                "map_name": config.carla.map_name,
                "vehicles": _states_payload(states),
            },
        )
        _print_json("CARLA已发现车辆", _states_payload(states))
        if mode == "carla-check":
            summary = {
                "mode": mode,
                "scenario_id": config.scenario_id,
                "configured_vehicle_count": len(config.vehicles),
                "discovered_vehicle_count": len(states),
                "status": (
                    "PASS" if len(states) == len(config.vehicles) else "INCOMPLETE"
                ),
                **monitoring_data,
            }
            recorder.write_json("summary.json", summary)
            export_interface(summary, recorder)
            print("\n连接检查完成：{}".format(recorder.run_dir))
            return

        if len(states) != len(config.vehicles):
            raise CarlaAdapterError(
                "Expected {} configured vehicles, discovered {}. "
                "Use --spawn-missing after checking the CARLA world.".format(
                    len(config.vehicles), len(states)
                )
            )

        runtime_zones = adapter.resolve_zones(config.zones)
        recorder.record(
            "runtime_scene",
            {
                "map_name": config.carla.map_name,
                "zones": _zones_payload(runtime_zones),
            },
        )
        assignments = scheduler.assign(tasks, states, runtime_zones)
        adapter.dispatch(tasks, runtime_zones)

        map_environment = adapter.get_map_environment()
        map_environment["zones"] = _zones_payload(runtime_zones)
        if monitoring_data:
            map_environment["monitoring_areas"] = monitoring_data.get(
                "monitoring_areas", []
            )
            map_environment["fixed_monitoring_stations"] = (
                monitoring_data.get("fixed_monitoring_stations", [])
            )

        # dispatch创建BasicAgent路线后重新读取一次状态，
        # 使首次地图快照即可携带规划路线。
        states = list(adapter.list_states())
        sync_runtime_state(
            states,
            tasks,
            assignments,
            {
                "type": "scheduler",
                "message": "CARLA初始任务调度完成",
            },
            run_id=recorder.run_id,
            environment=map_environment,
            monitoring=_runtime_monitoring_payload(
                monitoring_data,
                mobile_observation_count,
                phase="数据采集",
                phase_index=0,
                work_orders=work_orders,
                restrictions=restrictions,
                tasks=tasks,
                latest_event="固定站与移动装备开始协同采集",
            ),
        )
        _record_adapter_events(adapter, recorder)
        recorder.record(
            "initial_schedule",
            {"assignments": [item.to_dict() for item in assignments]},
        )
        _record_assignment_decisions(
            assignments,
            "initial_schedule",
            0,
            resolved_scenario,
            recorder,
            decision_records,
        )
        if risk_scenario is not None:
            recorder.record(
                "risk_dataset_loaded",
                {
                    "risk_scenario_id": risk_scenario.scenario_id,
                    "dataset_label": risk_scenario.dataset_label,
                    "synthetic_data": risk_scenario.synthetic_data,
                    "model_version": risk_scenario.model_version,
                },
            )
        failure_done = False
        risk_observations_by_tick = {
            item.tick: item
            for item in (
                risk_scenario.observations
                if risk_scenario is not None
                else []
            )
        }
        for tick_index in range(max(0, ticks)):
            if monitoring_layout is not None and tick_index % 20 == 0:
                mobile_states = list(adapter.list_states())
                recorded_count = _record_mobile_monitoring(
                    recorder,
                    monitoring_layout,
                    mobile_states,
                    tick_index,
                    risk_observation=risk_observations_by_tick.get(
                        tick_index
                    ),
                )
                mobile_observation_count += recorded_count
                if recorded_count:
                    mobile_equipment_ids.update(
                        item.vehicle_id for item in mobile_states
                    )
                    monitoring_data["mobile_equipment_count"] = len(
                        mobile_equipment_ids
                    )
                current_assessment = (
                    closed_loop_feedback_records[-1]["assessment"]
                    if closed_loop_feedback_records
                    else (
                        risk_assessments[-1]
                        if risk_assessments
                        else None
                    )
                )
                runtime_phase = (
                    "闭环完成"
                    if work_orders.all_closed()
                    else (
                        "复核反馈"
                        if closed_loop_feedback_records
                        else (
                            "装备执行"
                            if (
                                risk_task_ids
                                or hazard_takeover_triggered
                            )
                            else (
                                "风险分析"
                                if risk_assessments
                                else "数据采集"
                            )
                        )
                    )
                )
                runtime_phase_index = (
                    5
                    if work_orders.all_closed()
                    else (
                        4
                        if closed_loop_feedback_records
                        else (
                            3
                            if (
                                risk_task_ids
                                or hazard_takeover_triggered
                            )
                            else (1 if risk_assessments else 0)
                        )
                    )
                )
                sync_runtime_state(
                    mobile_states,
                    tasks,
                    run_id=recorder.run_id,
                    monitoring=_runtime_monitoring_payload(
                        monitoring_data,
                        mobile_observation_count,
                        phase=runtime_phase,
                        phase_index=runtime_phase_index,
                        work_orders=work_orders,
                        restrictions=restrictions,
                        tasks=tasks,
                        feedback_count=len(
                            closed_loop_feedback_records
                        ),
                        assessment=current_assessment,
                        latest_event=(
                            "第{}批移动监测数据已回传"
                        ).format(
                            mobile_observation_count
                        ),
                    ),
                )
            # 每2个CARLA tick读取一次UI命令，典型响应延迟约0.1~0.2秒。
            if tick_index % 2 == 0:
                processed_commands = process_runtime_commands(
                    adapter,
                    recorder,
                    tick_index,
                )
                if processed_commands:
                    current_states = list(adapter.list_states())
                    last_command = processed_commands[-1]
                    sync_runtime_state(
                        current_states,
                        tasks,
                        event={
                            "type": "command_execution",
                            "message": "{}：{}".format(
                                last_command["command"].get(
                                    "action", "command"
                                ),
                                last_command.get("status", "unknown"),
                            ),
                        },
                        run_id=recorder.run_id,
                    )

            if risk_scenario is not None and risk_engine is not None:
                for observation in observations_at_tick(
                    risk_scenario.observations, tick_index
                ):
                    recorder.record(
                        "risk_observation", observation.to_dict()
                    )
                    assessment = risk_engine.assess(observation)
                    risk_assessments.append(assessment)
                    recorder.record(
                        "risk_assessed", assessment.to_dict()
                    )
                    previous_slope_state = _record_slope_state_transition(
                        recorder, assessment, previous_slope_state
                    )
                    _record_risk_level_transition(recorder, assessment)
                    guidance = build_risk_guidance(
                        assessment,
                        risk_scenario,
                        resolved_scenario,
                    )
                    risk_guidance.append(guidance)
                    recorder.record(
                        "risk_guidance_generated", guidance
                    )
                    released_hazard_task_ids = []
                    emergency_event = config.scenario_variables.get(
                        "emergency_event", {}
                    )
                    configured_trigger_level = str(
                        (
                            emergency_event
                            if isinstance(emergency_event, dict)
                            else {}
                        ).get("trigger_level", "red")
                    ).lower()
                    if (
                        not hazard_takeover_triggered
                        and assessment.level == configured_trigger_level
                        and assessment.level != assessment.previous_level
                    ):
                        hazard_takeover_triggered = True
                        affected_vehicle_id = str(
                            (config.scenario_variables.get(
                                "slope_event", {}
                            ) or {}).get("affected_vehicle_id", "")
                        )
                        freeze_route = getattr(
                            adapter, "freeze_hazard_task_route", None
                        )
                        frozen_route_payload = (
                            freeze_route(affected_vehicle_id)
                            if affected_vehicle_id
                            and callable(freeze_route)
                            else None
                        )
                        if frozen_route_payload:
                            recorder.record(
                                "hazard_task_route_frozen",
                                {
                                    "tick": tick_index,
                                    **frozen_route_payload,
                                },
                            )
                        safe_hold_payload = (
                            _place_affected_vehicle_in_safe_hold(
                                adapter,
                                config,
                                assessment,
                                recorder,
                                tick_index,
                            )
                        )
                        released_hazard_task_ids = (
                            release_hazard_affected_tasks(
                                tasks,
                                affected_vehicle_id,
                                tick_index,
                            )
                            if safe_hold_payload is not None
                            else []
                        )
                        recorder.record(
                            "hazard_information_activated",
                            {
                                "tick": tick_index,
                                "risk_level": assessment.level,
                                "affected_vehicle_safe_hold": (
                                    safe_hold_payload
                                ),
                                "released_hazard_task_ids": (
                                    released_hazard_task_ids
                                ),
                                "road_closure_required": False,
                                "operating_rule": (
                                    "retain_slope_hazard_information_and_"
                                    "avoid_known_risk_area"
                                ),
                            },
                        )
                    restriction_action = risk_scenario.restriction
                    if (
                        restriction_action is not None
                        and assessment.level
                        in restriction_action.trigger_levels
                        and assessment.level != assessment.previous_level
                    ):
                        restriction = restrictions.activate(
                            assessment,
                            restriction_action,
                            tick=tick_index,
                        )
                        safe_route_payload = (
                            _activate_configured_safe_route(
                                adapter,
                                config,
                                restrictions,
                                restriction,
                                recorder,
                                tick_index,
                            )
                        )
                        cancelled_task_ids = (
                            restrictions.apply_to_tasks(tasks)
                        )
                        recorder.record(
                            "road_restriction_activated",
                            {
                                "tick": tick_index,
                                "restriction": (
                                    restriction.to_dict()
                                ),
                                "cancelled_task_ids": (
                                    cancelled_task_ids
                                ),
                                "safe_route": safe_route_payload,
                                "released_hazard_task_ids": (
                                    released_hazard_task_ids
                                ),
                            },
                        )
                    created = []
                    for action in actions_for_assessment(
                        risk_scenario, assessment
                    ):
                        task = create_risk_task(
                            assessment, action
                        )
                        order = work_orders.create_from_risk(
                            assessment,
                            task,
                            action,
                            tick=tick_index,
                        )
                        recorder.record(
                            "work_order_created",
                            order.to_dict(),
                        )
                        tasks.append(task)
                        created.append((task, action))
                    states = list(adapter.list_states())
                    affected_vehicle_id = str(
                        (config.scenario_variables.get(
                            "slope_event", {}
                        ) or {}).get("affected_vehicle_id", "")
                    )
                    handover_tasks = [
                        task for task in tasks
                        if task.handover_reason
                        and task.assigned_vehicle_id is None
                    ]
                    for handover_task in handover_tasks:
                        ranked_candidates = scheduler.rank_candidates(
                            handover_task,
                            states,
                            runtime_zones,
                            active_tasks=tasks,
                            excluded_vehicle_ids={affected_vehicle_id},
                        )
                        if not ranked_candidates:
                            raise SchedulingError(
                                "No eligible takeover vehicle for task {}"
                                .format(handover_task.task_id)
                            )
                        proposal = ranked_candidates[0]
                        handover_task.recommended_vehicle_id = (
                            proposal.vehicle_id
                        )
                        handover_task.recommendation_reason = (
                            proposal.reason
                        )
                        handover_task.candidate_evaluations = [
                            item.to_dict() for item in ranked_candidates
                        ]
                        handover_task.status = "pending"
                        handover_task.status_reason = (
                            "awaiting_human_approval_for_ai_takeover"
                        )
                        recorder.record(
                            "hazard_task_takeover_proposed",
                            {
                                "tick": tick_index,
                                "task_id": handover_task.task_id,
                                "original_vehicle_id": (
                                    handover_task.original_vehicle_id
                                ),
                                "recommended_vehicle_id": (
                                    proposal.vehicle_id
                                ),
                                "ai_recommendation": proposal.to_dict(),
                                "candidate_evaluations": [
                                    item.to_dict()
                                    for item in ranked_candidates
                                ],
                                "requires_human_approval": True,
                            },
                        )
                    if not created:
                        if handover_tasks:
                            sync_runtime_state(
                                states,
                                tasks,
                                event={
                                    "type": "hazard_takeover_decision",
                                    "message": (
                                        "边坡异常已记录；矿车1安全停车，"
                                        "AI已完成候选矿车择优，等待人工确认"
                                    ),
                                },
                                run_id=recorder.run_id,
                                risk=assessment.to_dict(),
                                monitoring=_runtime_monitoring_payload(
                                    monitoring_data,
                                    mobile_observation_count,
                                    phase="任务调度",
                                    phase_index=2,
                                    work_orders=work_orders,
                                    restrictions=restrictions,
                                    tasks=tasks,
                                    feedback_count=len(
                                        closed_loop_feedback_records
                                    ),
                                    assessment=assessment,
                                    latest_event=(
                                        "边坡异常持续标记；等待确认AI任务接管方案"
                                    ),
                                ),
                            )
                        continue
                    schedulable_tasks = [
                        task for task in tasks
                        if task not in handover_tasks
                    ]
                    risk_assignments = scheduler.assign(
                        schedulable_tasks,
                        states,
                        runtime_zones,
                        excluded_vehicle_ids=(
                            {affected_vehicle_id}
                            if affected_vehicle_id
                            else set()
                        ),
                    )
                    sync_runtime_state(
                        states,
                        tasks,
                        risk_assignments,
                        {
                            "type": "risk_response",
                            "message": "CARLA风险触发，重新生成任务并调度",
                        },
                        run_id=recorder.run_id,
                        risk=assessment.to_dict(),
                        monitoring=_runtime_monitoring_payload(
                            monitoring_data,
                            mobile_observation_count,
                            phase="任务调度",
                            phase_index=2,
                            work_orders=work_orders,
                            restrictions=restrictions,
                            tasks=tasks,
                            feedback_count=len(
                                closed_loop_feedback_records
                            ),
                            assessment=assessment,
                            latest_event=(
                                (
                                    "突发事件：强降雨诱发东帮"
                                    "边坡失稳；已生成{}个处置"
                                    "工单并下发安全路线"
                                ).format(len(created))
                                if assessment.level == "red"
                                else "{}风险触发，已生成{}"
                                "个处置工单".format(
                                    assessment.level.upper(),
                                    len(created),
                                )
                            ),
                        ),
                    )
                    assignment_by_task = {
                        item.task_id: item
                        for item in risk_assignments
                    }
                    for task, _ in created:
                        risk_assignment = assignment_by_task[
                            task.task_id
                        ]
                        transition = work_orders.assign(
                            task.task_id,
                            risk_assignment.vehicle_id,
                            tick=tick_index,
                        )
                        _record_work_order_transitions(
                            [transition], recorder
                        )
                        risk_task_ids.append(task.task_id)
                        recorder.record(
                            "risk_task_created",
                            {
                                "tick": tick_index,
                                "assessment": (
                                    assessment.to_dict()
                                ),
                                "task": task.to_dict(),
                                "assignment": (
                                    risk_assignment.to_dict()
                                ),
                            },
                        )
                    _record_assignment_decisions(
                        [
                            assignment_by_task[task.task_id]
                            for task, _ in created
                        ],
                        "risk_response",
                        tick_index,
                        resolved_scenario,
                        recorder,
                        decision_records,
                    )
                    adapter.dispatch(tasks, runtime_zones)
                    adapter_events = _record_adapter_events(
                        adapter, recorder
                    )
                    _record_work_order_transitions(
                        work_orders.process_adapter_events(
                            adapter_events
                        ),
                        recorder,
                    )
            if (
                inject
                and not failure_done
                and tick_index == failure_tick
            ):
                failed = failure_vehicle_id
                adapter.inject_fault(failed)
                released = release_failed_vehicle_tasks(tasks, failed)
                states = list(adapter.list_states())
                reassigned = scheduler.assign(
                    tasks,
                    states,
                    runtime_zones,
                    excluded_vehicle_ids={failed},
                )
                sync_runtime_state(
                    states,
                    tasks,
                    reassigned,
                    {
                        "type": "fault_recovery",
                        "message": "{}故障，任务重新分配".format(failed),
                    },
                    run_id=recorder.run_id,
                )
                adapter.dispatch(tasks, runtime_zones)
                _record_adapter_events(adapter, recorder)
                recorder.record(
                    "vehicle_fault",
                    {
                        "tick": tick_index,
                        "vehicle_id": failed,
                        "scenario_seed": resolved_scenario.seed,
                        "released_task_ids": released,
                        "reassignments": [
                            item.to_dict() for item in reassigned
                        ],
                    },
                )
                _record_assignment_decisions(
                    reassigned,
                    "failure_recovery",
                    tick_index,
                    resolved_scenario,
                    recorder,
                    decision_records,
                )
                failure_done = True
            task_status = adapter.tick()
            adapter_events = _record_adapter_events(adapter, recorder)

            if tick_index % 5 == 0:
                current_states = list(adapter.list_states())
                sync_runtime_state(
                    current_states,
                    tasks,
                    run_id=recorder.run_id,
                )

            _record_work_order_transitions(
                work_orders.process_adapter_events(adapter_events),
                recorder,
            )
            if risk_scenario is not None:
                feedback_transitions, feedback_records = (
                    _process_closed_loop_feedback(
                        work_orders,
                        risk_engine,
                        risk_scenario,
                        recorder,
                        tick_index,
                    )
                )
                _record_work_order_transitions(
                    feedback_transitions, recorder
                )
                closed_loop_feedback_records.extend(
                    feedback_records
                )
                if feedback_records and work_orders.all_closed():
                    deactivated_restrictions = (
                        restrictions.deactivate_all(
                            tick_index,
                            "all_feedback_reviews_safe",
                        )
                    )
                    for restriction in deactivated_restrictions:
                        recorder.record(
                            "road_restriction_deactivated",
                            restriction.to_dict(),
                        )
                    if deactivated_restrictions:
                        clear_safe_route = getattr(
                            adapter, "clear_safe_route", None
                        )
                        if callable(clear_safe_route):
                            cleared_route = clear_safe_route(
                                "all_feedback_reviews_safe"
                            )
                            if cleared_route:
                                recorder.record(
                                    "safe_route_deactivated",
                                    {
                                        "tick": tick_index,
                                        **cleared_route,
                                    },
                                )
                if feedback_records:
                    feedback_assessment = feedback_records[-1][
                        "assessment"
                    ]
                    sync_runtime_state(
                        list(adapter.list_states()),
                        tasks,
                        event={
                            "type": "closed_loop_feedback",
                            "message": (
                                "复核数据已回传，风险降为{}，"
                                "工单已自动处理"
                            ).format(
                                feedback_assessment.get(
                                    "level", "unknown"
                                )
                            ),
                        },
                        run_id=recorder.run_id,
                        risk=feedback_assessment,
                        monitoring=_runtime_monitoring_payload(
                            monitoring_data,
                            mobile_observation_count,
                            phase=(
                                "闭环完成"
                                if work_orders.all_closed()
                                else "复核反馈"
                            ),
                            phase_index=(
                                5 if work_orders.all_closed() else 4
                            ),
                            work_orders=work_orders,
                            restrictions=restrictions,
                            tasks=tasks,
                            feedback_count=len(
                                closed_loop_feedback_records
                            ),
                            assessment=feedback_assessment,
                            latest_event=(
                                "复核风险{}，工单{}"
                            ).format(
                                feedback_assessment.get(
                                    "level", "unknown"
                                ).upper(),
                                (
                                    "已全部关闭"
                                    if work_orders.all_closed()
                                    else "持续处理"
                                ),
                            ),
                        ),
                    )
            if tick_index % 20 == 0:
                recorder.record(
                    "carla_tick",
                    {
                        "tick": tick_index,
                        "task_status": task_status,
                        "vehicles": _states_payload(
                            list(adapter.list_states())
                        ),
                    },
                )
            result = _run_result(tasks)
            risk_schedule_complete = (
                risk_scenario is None
                or (
                    last_risk_tick is not None
                    and tick_index >= last_risk_tick
                    and (
                        bool(risk_task_ids)
                        or hazard_takeover_triggered
                    )
                )
            )
            if (
                result["all_tasks_terminal"]
                and (not inject or failure_done)
                and risk_schedule_complete
                and (
                    risk_scenario is None
                    or work_orders.all_terminal()
                    or (
                        hazard_takeover_triggered
                        and risk_scenario.restriction is None
                    )
                )
            ):
                recorder.record(
                    "all_tasks_terminal",
                    {"tick": tick_index, "result": result},
                )
                break

        result = _run_result(tasks)
        risk_schedule_complete = (
            risk_scenario is None
            or (
                last_risk_tick is not None
                and ticks > last_risk_tick
                and (
                    bool(risk_task_ids)
                    or hazard_takeover_triggered
                )
            )
        )
        if not risk_schedule_complete and result["status"] == "PASS":
            result["status"] = "PARTIAL"
        if (
            risk_scenario is not None
            and not work_orders.all_terminal()
            and not (
                hazard_takeover_triggered
                and risk_scenario.restriction is None
            )
            and result["status"] == "PASS"
        ):
            result["status"] = "PARTIAL"
        terminal_settle_ticks = 0
        if result["all_tasks_terminal"]:
            terminal_states = []
            for _ in range(120):
                adapter.tick()
                terminal_settle_ticks += 1
                terminal_states = list(adapter.list_states())
                if all(
                    state.speed_mps <= 0.05
                    for state in terminal_states
                ):
                    break
            recorder.record(
                "terminal_state_settled",
                {
                    "settle_ticks": terminal_settle_ticks,
                    "all_vehicles_stopped": all(
                        state.speed_mps <= 0.05
                        for state in terminal_states
                    ),
                    "vehicles": _states_payload(terminal_states),
                },
            )
        else:
            terminal_states = list(adapter.list_states())
        risk_response_metrics = []
        assessment_ticks = {
            item.assessment_id: item.tick for item in risk_assessments
        }
        for task in tasks:
            if not task.source_event_id:
                continue
            trigger_tick = assessment_ticks.get(task.source_event_id)
            risk_response_metrics.append(
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "source_event_id": task.source_event_id,
                    "trigger_tick": trigger_tick,
                    "started_tick": task.started_tick,
                    "completed_tick": task.completed_tick,
                    "dispatch_latency_ticks": (
                        task.started_tick - trigger_tick
                        if task.started_tick is not None
                        and trigger_tick is not None
                        else None
                    ),
                    "completion_latency_ticks": (
                        task.completed_tick - trigger_tick
                        if task.completed_tick is not None
                        and trigger_tick is not None
                        else None
                    ),
                }
            )
        if monitoring_data:
            monitoring_data["mobile_equipment_count"] = len(
                mobile_equipment_ids
            )
            monitoring_data["mobile_observation_count"] = (
                mobile_observation_count
            )
        closed_loop_decision_counts = dict(
            Counter(
                item["decision"]
                for item in closed_loop_feedback_records
            )
        )
        takeover_tasks = [
            task for task in tasks if task.handover_reason
        ]
        takeover_completed = bool(takeover_tasks) and all(
            task.status == "completed" for task in takeover_tasks
        )
        summary = {
            "mode": mode,
            "scenario_id": config.scenario_id,
            "run_id": recorder.run_id,
            "scenario_seed": resolved_scenario.seed,
            "scenario_mode": resolved_scenario.mode,
            "imitation_memory": imitation_memory,
            "instruction": config.instruction,
            "mission": resolved_scenario.mission,
            "max_ticks": ticks,
            "ticks_executed": tick_index + 1 if ticks > 0 else 0,
            "terminal_settle_ticks": terminal_settle_ticks,
            "failure_injected": failure_done,
            "risk_scenario_id": (
                risk_scenario.scenario_id
                if risk_scenario is not None
                else None
            ),
            "risk_dataset_label": (
                risk_scenario.dataset_label
                if risk_scenario is not None
                else None
            ),
            "risk_assessments": [
                item.to_dict() for item in risk_assessments
            ],
            "risk_guidance": risk_guidance,
            "risk_task_ids": risk_task_ids,
            "risk_triggered": bool(risk_assessments),
            "risk_schedule_complete": risk_schedule_complete,
            "hazard_information_retained": (
                hazard_takeover_triggered
            ),
            "road_restriction_required": bool(
                risk_scenario is not None
                and risk_scenario.restriction is not None
            ),
            "takeover_completed": takeover_completed,
            "risk_response_metrics": risk_response_metrics,
            "closed_loop_feedback_count": len(
                closed_loop_feedback_records
            ),
            "closed_loop_feedback_records": (
                closed_loop_feedback_records
            ),
            "closed_loop_decision_counts": (
                closed_loop_decision_counts
            ),
            "monitoring_dispatch_closed_loop": (
                takeover_completed
                or (
                    bool(closed_loop_feedback_records)
                    and work_orders.all_terminal()
                )
            ),
            "feedback_artifact": "feedback_observations.jsonl",
            "tasks": _tasks_payload(tasks),
            "zones": _zones_payload(runtime_zones),
            "vehicle_states": _states_payload(terminal_states),
            **monitoring_data,
            **result,
            **(
                {
                    **work_orders.summary(),
                    **restrictions.summary(),
                }
                if risk_scenario is not None
                else {}
            ),
        }
        sync_runtime_state(
            terminal_states,
            tasks,
            event={
                "type": "run_completed",
                "message": "CARLA运行结束",
            },
            run_id=recorder.run_id,
            decision={"status": summary.get("status", "UNKNOWN")},
            monitoring=_runtime_monitoring_payload(
                monitoring_data,
                mobile_observation_count,
                phase=(
                    "闭环完成"
                    if summary.get("monitoring_dispatch_closed_loop")
                    else "运行结束"
                ),
                phase_index=(
                    5
                    if summary.get("monitoring_dispatch_closed_loop")
                    else 4
                ),
                work_orders=work_orders,
                restrictions=restrictions,
                tasks=tasks,
                feedback_count=len(closed_loop_feedback_records),
                assessment=(
                    closed_loop_feedback_records[-1]["assessment"]
                    if closed_loop_feedback_records
                    else (
                        risk_assessments[-1]
                        if risk_assessments
                        else None
                    )
                ),
                latest_event="CARLA综合演示运行结束",
            ),
        )
        finalize_learning_and_acceptance(
            recorder, summary, decision_records
        )
        recorder.record("run_completed", summary)
        recorder.write_json("summary.json", summary)
        export_interface(summary, recorder)
        print("\nCARLA运行结束：{}".format(recorder.run_dir))
    finally:
        adapter.close()


def main() -> None:
    args = build_parser().parse_args()
    recorder = None
    try:
        config = load_config(args.config)
        risk_scenario = (
            load_risk_scenario(args.risk_config)
            if args.risk_config is not None
            else None
        )
        monitoring_layout = (
            load_monitoring_layout(args.monitoring_config)
            if args.monitoring_config is not None
            else None
        )
        if args.seed is not None and args.randomize:
            raise ConfigError(
                "--seed and --randomize cannot be used together"
            )
        resolved_scenario = resolve_scenario(
            config,
            seed_override=args.seed,
            randomize=args.randomize,
        )
        if args.inject_failure and not config.demo.failure_enabled:
            raise ConfigError(
                "--inject-failure cannot be used when demo.failure_enabled is false"
            )
        if risk_scenario is not None:
            zone_ids = {zone.zone_id for zone in config.zones}
            referenced_zone_ids = {
                risk_scenario.action.zone_id,
                *[
                    observation.zone_id
                    for observation in risk_scenario.observations
                ],
                *[
                    action.zone_id
                    for action in risk_scenario.additional_actions
                ],
            }
            if risk_scenario.restriction is not None:
                referenced_zone_ids.add(
                    risk_scenario.restriction.zone_id
                )
            missing_zone_ids = referenced_zone_ids.difference(zone_ids)
            if missing_zone_ids:
                raise RiskConfigError(
                    "Risk config references unknown zones: {}".format(
                        sorted(missing_zone_ids)
                    )
                )
        recorder = EvidenceRecorder(args.artifacts, config.scenario_id)
        snapshot = resolved_scenario.to_dict()
        recorder.write_json("scenario_snapshot.json", snapshot)
        recorder.record("scenario_snapshot_created", snapshot)
        failure_plan = resolved_scenario.failure_plan(config)
        episode = build_episode(
            config,
            run_id=recorder.run_id,
            seed=resolved_scenario.seed,
            realized_events=resolved_scenario.realized_events,
            failure_plan=failure_plan,
        )
        recorder.write_json("episode.json", episode.to_dict())
        recorder.record_episode(episode, config_path=args.config)
        if args.mode == "mock":
            run_mock(
                config,
                recorder,
                inject=args.inject_failure,
                risk_scenario=risk_scenario,
                monitoring_layout=monitoring_layout,
                resolved_scenario=resolved_scenario,
                realtime_delay=args.realtime_delay,
            )
        else:
            run_carla(
                config=config,
                recorder=recorder,
                mode=args.mode,
                load_map=args.load_map,
                spawn_missing=args.spawn_missing,
                ticks=args.ticks,
                inject=args.inject_failure,
                risk_scenario=risk_scenario,
                monitoring_layout=monitoring_layout,
                resolved_scenario=resolved_scenario,
            )
    except (
        ConfigError,
        MonitoringConfigError,
        RiskConfigError,
        SchedulingError,
        CarlaAdapterError,
        WorkOrderError,
        OSError,
    ) as exc:
        raise SystemExit("ERROR: {}".format(exc))
    finally:
        if recorder is not None:
            recorder.close()


if __name__ == "__main__":
    main()
