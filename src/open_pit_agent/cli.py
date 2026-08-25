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
from .risk import (
    RiskConfigError,
    RiskScenario,
    RuleBasedRiskEngine,
    actions_for_assessment,
    create_risk_task,
    load_risk_scenario,
    observations_at_tick,
)
from .restrictions import RestrictionRegistry
from .scenario_runtime import ResolvedScenario, resolve_scenario
from .scheduler import (
    BaselineScheduler,
    SchedulingError,
    release_failed_vehicle_tasks,
    tasks_from_zones,
)
RUNTIME_API_URL = os.environ.get(
    "OPENPIT_RUNTIME_API_URL",
    "http://127.0.0.1:8000/runtime/sync",
)
RUNTIME_API_BASE_URL = RUNTIME_API_URL.rsplit(
    "/runtime/sync", 1
)[0]

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

    if environment is not None:
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


def run_mock(
    config: ScenarioConfig,
    recorder: EvidenceRecorder,
    inject: bool,
    risk_scenario: Optional[RiskScenario],
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
                recorder.record("risk_observation", observation.to_dict())
                assessment = engine.assess(observation)
                risk_assessments.append(assessment)
                recorder.record("risk_assessed", assessment.to_dict())
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

        released = []
        reassigned = []
        if inject:
            failed, failure_tick = (
                resolved_scenario.failure_plan(config)
            )
            adapter.inject_fault(failed)
            released = release_failed_vehicle_tasks(tasks, failed)
            states = list(adapter.list_states())

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
    risk_guidance = []
    decision_records = []
    failure_vehicle_id, failure_tick = (
        resolved_scenario.failure_plan(config)
    )
    last_risk_tick = (
        max(item.tick for item in risk_scenario.observations)
        if risk_scenario is not None
        else None
    )
    adapter.connect()
    try:
        adapter.ensure_vehicles(spawn_missing=spawn_missing)
        states = list(adapter.list_states())
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
        for tick_index in range(max(0, ticks)):
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
                    guidance = build_risk_guidance(
                        assessment,
                        risk_scenario,
                        resolved_scenario,
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
                    ):
                        restriction = restrictions.activate(
                            assessment,
                            restriction_action,
                            tick=tick_index,
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
                    if not created:
                        continue
                    states = list(adapter.list_states())
                    risk_assignments = scheduler.assign(
                        tasks, states, runtime_zones
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
                _record_work_order_transitions(
                    work_orders.auto_review(tick_index),
                    recorder,
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
                    and bool(risk_task_ids)
                )
            )
            if (
                result["all_tasks_terminal"]
                and (not inject or failure_done)
                and risk_schedule_complete
                and (
                    risk_scenario is None
                    or work_orders.all_terminal()
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
                and bool(risk_task_ids)
            )
        )
        if not risk_schedule_complete and result["status"] == "PASS":
            result["status"] = "PARTIAL"
        if (
            risk_scenario is not None
            and not work_orders.all_terminal()
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
            "risk_triggered": bool(risk_task_ids),
            "risk_schedule_complete": risk_schedule_complete,
            "risk_response_metrics": risk_response_metrics,
            "tasks": _tasks_payload(tasks),
            "zones": _zones_payload(runtime_zones),
            "vehicle_states": _states_payload(terminal_states),
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
    try:
        config = load_config(args.config)
        risk_scenario = (
            load_risk_scenario(args.risk_config)
            if args.risk_config is not None
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
        if args.mode == "mock":
            run_mock(
                config,
                recorder,
                inject=args.inject_failure,
                risk_scenario=risk_scenario,
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
                resolved_scenario=resolved_scenario,
            )
    except (
        ConfigError,
        RiskConfigError,
        SchedulingError,
        CarlaAdapterError,
        WorkOrderError,
        OSError,
    ) as exc:
        raise SystemExit("ERROR: {}".format(exc))


if __name__ == "__main__":
    main()
