"""Explainable decisions, reusable experience data and run acceptance."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Assignment, Task
from .risk import RiskAssessment, RiskScenario, actions_for_assessment
from .scenario_runtime import ResolvedScenario
from .sqlite_store import SqliteRunStore


POLICY_VERSION = "capability-distance-load-transparent-v1"
GUIDANCE_VERSION = "slope-risk-guidance-demo-v1"
EXPERIENCE_SCHEMA_VERSION = "1.0"
STRUCTURAL_TRANSITION_SCHEMA_VERSION = "openpit-structural-transition-v1"
CLOSED_LOOP_TRANSITION_SCHEMA_VERSION = "openpit-closed-loop-transition-v1"
WORLD_STATE_SCHEMA_VERSION = "openpit.world-state.v1"
DECISION_ACTION_SCHEMA_VERSION = "openpit.decision-action.v1"
EXECUTION_FEEDBACK_SCHEMA_VERSION = "openpit.execution-feedback.v1"
GLOBAL_ASSIGNMENT_SCHEMA_VERSION = "openpit-global-assignment-v2"
MAX_IMITATION_BONUS_M = 2.0


def _closed_loop_done(next_state: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """Derive an episode boundary only when task state proves it."""
    tasks = next_state.get("tasks") if isinstance(next_state, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return None, "NOT_AVAILABLE_NO_TASK_STATE"
    statuses = {
        str(item.get("status") or "").lower()
        for item in tasks if isinstance(item, dict)
    }
    if not statuses:
        return None, "NOT_AVAILABLE_NO_TASK_STATUS"
    terminal = {"completed", "cancelled", "failed", "timed_out"}
    return statuses.issubset(terminal), "DERIVED_FROM_NEXT_STATE_TASK_STATUS"


def _build_closed_loop_transition(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Convert one stored V3 cycle without inventing unavailable values."""
    reasons = list(row.get("load_errors") or [])
    state = row.get("state_before")
    next_state = row.get("next_state")
    feedback = row.get("feedback")
    command = row.get("command")
    if row.get("schema_version") != "openpit.closed-loop-cycle.v1":
        reasons.append("unsupported_cycle_schema")
    if not isinstance(state, dict) or state.get("schema_version") != WORLD_STATE_SCHEMA_VERSION:
        reasons.append("invalid_state_before")
    if not isinstance(next_state, dict) or next_state.get("schema_version") != WORLD_STATE_SCHEMA_VERSION:
        reasons.append("invalid_next_state")
    if not isinstance(feedback, list):
        reasons.append("invalid_execution_feedback")
    if not isinstance(command, dict):
        reasons.append("invalid_execution_command")
    if reasons:
        return None, sorted(set(reasons))

    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    scheduling = row.get("scheduling") if isinstance(row.get("scheduling"), dict) else {}
    route = row.get("route") if isinstance(row.get("route"), dict) else {}
    safety = row.get("safety") if isinstance(row.get("safety"), dict) else {}
    summary = row.get("run_summary") if isinstance(row.get("run_summary"), dict) else {}
    environment = state.get("environment") if isinstance(state.get("environment"), dict) else {}
    map_context = environment.get("map_context")
    if not isinstance(map_context, dict):
        map_context = summary.get("map_context")
    if not isinstance(map_context, dict):
        map_context = {}
    done, done_status = _closed_loop_done(next_state)
    safety_status = str(safety.get("status") or "UNKNOWN").upper()
    action_type = command.get("action_type")
    policy_version = (
        decision.get("policy_version") or summary.get("policy_version")
        or command.get("issued_by")
    )
    feedback_schemas_valid = all(
        isinstance(item, dict)
        and item.get("schema_version") == EXECUTION_FEEDBACK_SCHEMA_VERSION
        for item in feedback
    )
    action = {
        "schema_version": DECISION_ACTION_SCHEMA_VERSION,
        "action_type": action_type,
        "task_ids": command.get("task_ids", []),
        "assignments": command.get("assignments", {}),
        "policy_version": policy_version,
        "decision": decision,
        "scheduling": scheduling,
        "route_plan": route,
        "safety_review": safety,
        "execution_command": command,
    }
    record = {
        "schema_version": CLOSED_LOOP_TRANSITION_SCHEMA_VERSION,
        "experience_id": "{}:{}".format(row.get("run_id"), row.get("cycle_id")),
        "episode": {
            "run_id": row.get("run_id"),
            "scenario_id": row.get("scenario_id"),
            "scenario_key": row.get("scenario_key"),
            "seed": row.get("scenario_seed"),
            "scenario_mode": row.get("scenario_mode"),
            "simulation_mode": row.get("simulator_mode"),
            "run_status": row.get("run_status"),
        },
        "cycle": {
            "cycle_id": row.get("cycle_id"),
            "status": row.get("status"),
            "revision_before": row.get("revision_before"),
            "revision_after": row.get("revision_after"),
            "trace": row.get("trace"),
        },
        "map_context": map_context,
        "state": state,
        "action": action,
        "result": {
            "cycle_status": row.get("status"),
            "execution_feedback": feedback,
            "measurement_status": row.get("measurement_status"),
            "physical_execution": bool(row.get("physical_execution")),
        },
        "next_state": next_state,
        "reward": None,
        "reward_status": "NOT_AVAILABLE_NO_REWARD_MODEL",
        "reward_detail": {},
        "done": done,
        "done_status": done_status,
        "safety_override": safety_status == "MODIFIED",
        "policy_version": policy_version,
        "data_quality": {
            "transition_granularity": "direct_closed_loop_cycle",
            "source": "openpit.db.closed_loop_cycles",
            "state_schema_valid": True,
            "next_state_schema_valid": True,
            "feedback_schema_valid": feedback_schemas_valid,
            "physical_execution": bool(row.get("physical_execution")),
            "measurement_status": row.get("measurement_status"),
            "real_mine_data": False,
            "eligible_for_behavior_cloning_preparation": bool(
                action_type and feedback_schemas_valid
                and row.get("status") == "SUCCEEDED"
                and safety_status in ("APPROVED", "MODIFIED", "PASS")
            ),
            "compatible_with_current_task_level_bc_trainer": False,
            "eligible_for_ppo_training": False,
        },
    }
    return record, []


def export_closed_loop_transition_dataset(
    database_path: Path,
    datasets_root: Path,
    dataset_id: str,
    scenario_keys: Optional[Sequence[str]] = None,
    run_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Export direct V3 cycles as an immutable JSONL dataset and manifest."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", str(dataset_id)):
        raise ValueError("dataset id must use 1-80 safe filename characters")
    database_path = Path(database_path).expanduser().resolve()
    if not database_path.is_file():
        raise ValueError("openpit database does not exist: {}".format(database_path))
    store = SqliteRunStore(database_path)
    try:
        rows = store.load_closed_loop_cycles(
            scenario_keys=scenario_keys, run_ids=run_ids
        )
    finally:
        store.close()
    records = []
    skipped = []
    for row in rows:
        record, reasons = _build_closed_loop_transition(row)
        if record is None:
            skipped.append({
                "run_id": row.get("run_id"), "cycle_id": row.get("cycle_id"),
                "reasons": reasons,
            })
        else:
            records.append(record)
    output_dir = (
        Path(datasets_root) / CLOSED_LOOP_TRANSITION_SCHEMA_VERSION / dataset_id
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    transitions_path = output_dir / "transitions.jsonl"
    with transitions_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def counts(values):
        result = {}
        for value in values:
            key = str(value if value is not None else "NOT_AVAILABLE")
            result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items()))

    experience_ids = [item["experience_id"] for item in records]
    checks = [
        {"check": "records_present", "passed": bool(records), "detail": len(records)},
        {"check": "stored_rows_parseable", "passed": not skipped, "detail": len(skipped)},
        {"check": "experience_ids_unique",
         "passed": len(experience_ids) == len(set(experience_ids)),
         "detail": len(experience_ids) - len(set(experience_ids))},
        {"check": "source_runs_passed", "passed": all(
            item["episode"].get("run_status") == "PASS" for item in records
        ), "detail": counts(item["episode"].get("run_status") for item in records)},
    ]
    quality_status = (
        "DATASET_QUALITY_PASS" if all(item["passed"] for item in checks)
        else "DATASET_QUALITY_WARN"
    )
    manifest = {
        "schema_version": CLOSED_LOOP_TRANSITION_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_database": str(database_path),
        "source_table": "closed_loop_cycles",
        "scenario_filter": sorted(set(scenario_keys or ())),
        "run_id_filter": sorted(set(run_ids or ())),
        "stored_cycle_count": len(rows),
        "record_count": len(records),
        "skipped_cycle_count": len(skipped),
        "skipped_cycles": skipped,
        "source_run_count": len(set(item["episode"]["run_id"] for item in records)),
        "scenario_record_counts": counts(
            item["episode"].get("scenario_key") for item in records
        ),
        "cycle_status_counts": counts(
            item["cycle"].get("status") for item in records
        ),
        "measurement_status_counts": counts(
            item["result"].get("measurement_status") for item in records
        ),
        "physical_execution_record_count": sum(
            bool(item["result"].get("physical_execution")) for item in records
        ),
        "reward_summary": {
            "available_record_count": 0,
            "unavailable_record_count": len(records),
            "status": "NOT_AVAILABLE_NO_REWARD_MODEL",
        },
        "done_available_record_count": sum(
            item.get("done") is not None for item in records
        ),
        "behavior_cloning_preparation_eligible_count": sum(
            bool(item["data_quality"].get(
                "eligible_for_behavior_cloning_preparation"
            )) for item in records
        ),
        "ppo_eligible_record_count": 0,
        "dataset_quality": {"status": quality_status, "checks": checks},
        "transition_contract": {
            "state": WORLD_STATE_SCHEMA_VERSION,
            "action": DECISION_ACTION_SCHEMA_VERSION,
            "feedback": EXECUTION_FEEDBACK_SCHEMA_VERSION,
            "next_state": WORLD_STATE_SCHEMA_VERSION,
            "reward": "null_until_explicit_reward_model",
        },
        "transitions_path": str(transitions_path),
        "limitations": [
            "cycle_level_actions_not_yet_supported_by_current_task_level_bc_trainer",
            "no_reward_model_so_not_valid_for_ppo_training",
            "structural_feedback_is_not_carla_physical_measurement",
            "synthetic_scenarios_are_not_real_mine_data",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_structural_reward_config(path: Path) -> Dict[str, Any]:
    """Load explicit non-optimal structural reward weights."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    config = payload.get("structural_reward")
    if not isinstance(config, dict):
        raise ValueError("decision config requires structural_reward")
    weights = config.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("structural_reward requires non-empty weights")
    normalized = {str(name): float(value) for name, value in weights.items()}
    if any(value < 0 for value in normalized.values()) or not any(normalized.values()):
        raise ValueError("structural reward weights must be non-negative and not all zero")
    return {
        "reward_version": str(config.get("reward_version")),
        "weight_status": str(config.get("weight_status")),
        "normalization": str(config.get("normalization")),
        "weights": normalized,
    }


def _evaluate_structural_reward(
    record: Dict[str, Any],
    result: Dict[str, Any],
    reward_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate only observable structural terminal outcomes."""
    if reward_config is None:
        return {
            "value": None,
            "status": "NOT_AVAILABLE_NO_REWARD_MODEL",
            "reward_version": None,
            "components": {},
        }

    action = record["action"]
    scenario_key = record["episode"]["scenario_key"]
    task_status = record["result"].get("task_status")
    evidence_status = record["result"].get("database_evidence_status")
    components = {
        "task_completion": {
            "value": 1.0 if task_status == "completed" else -1.0,
            "availability": "available",
            "source": "task.status",
        },
        "closed_loop_evidence": {
            "value": (
                1.0 if evidence_status == "EVIDENCE_PASS"
                else -1.0 if evidence_status == "EVIDENCE_FAIL"
                else None
            ),
            "availability": (
                "available" if evidence_status in ("EVIDENCE_PASS", "EVIDENCE_FAIL")
                else "not_available"
            ),
            "source": "openpit.db.closed_loop_evidence_validation",
        },
        "scenario_resolution": {
            "value": None, "availability": "not_available",
            "source": "scenario_specific_structural_result",
        },
        "switch_cost": {
            "value": None, "availability": "not_available",
            "source": "action.original_vehicle_id_vs_selected_vehicle_id",
        },
        "detour_cost": {
            "value": None, "availability": "not_available",
            "source": "route.original_distance_m_vs_replanned_distance_m",
        },
    }
    if scenario_key == "s02":
        failed = result.get("failed_vehicle_id")
        released = set(result.get("released_task_ids", []))
        resolved = (
            action.get("task_id") in released
            and action.get("selected_vehicle_id") != failed
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "fault_release_reassignment_terminal_result",
        })
    elif scenario_key == "s03":
        route = action.get("route", {})
        resolved = (
            action.get("action_type") == "switch_to_alternative_work_point"
            and route.get("failed_work_point_id")
                != route.get("alternative_work_point_id")
            and action.get("constraint_results", {}).get(
                "alternative_route_reachable") is True
            and result.get("equipment_status_after_recovery") == "AVAILABLE"
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "equipment_failure_work_point_switch_terminal_result",
        })
    elif scenario_key == "s04":
        route = action.get("route", {})
        restricted_edge = route.get("restricted_edge_id")
        action_type = action.get("action_type")
        resolved = (
            bool(restricted_edge)
            and action_type in {
                "blast_zone_safe_route", "hold_until_blast_clearance"
            }
            and (
                action_type == "hold_until_blast_clearance"
                or restricted_edge not in route.get("replanned_edge_ids", [])
            )
            and result.get("blast_zone_status_after_clearance") == "CLEARED"
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "planned_blast_control_terminal_result",
        })
    elif scenario_key == "s07":
        route = action.get("route", {})
        closed_edge = route.get("closed_edge_id")
        resolved = (
            bool(closed_edge)
            and closed_edge not in route.get("replanned_edge_ids", [])
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "closed_edge_avoidance_terminal_result",
        })
    elif scenario_key == "s05":
        degraded_edge = action.get("route", {}).get("degraded_edge_id")
        selected_edges = action.get("route", {}).get("selected_edge_ids", [])
        action_type = action.get("action_type")
        resolved = (
            bool(degraded_edge)
            and action_type in {
                "weather_safe_route_replan", "weather_speed_restriction"
            }
            and (
                action_type == "weather_speed_restriction"
                or degraded_edge not in selected_edges
            )
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "weather_road_capacity_response_terminal_result",
        })
    elif scenario_key == "s06":
        traffic = action.get("traffic", {})
        resolved = (
            action.get("action_type") in {
                "hold_for_safe_headway", "authorize_bottleneck_entry"
            }
            and traffic.get("scheduled_entry_s") is not None
            and traffic.get("scheduled_exit_s") is not None
            and result.get("traffic_control_status_after_recovery") == "RELEASED"
            and task_status == "completed"
        )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "capacity_headway_schedule_terminal_result",
        })
    elif scenario_key == "s09":
        route = action.get("route", {})
        closed_edge = route.get("closed_edge_id")
        if action.get("action_type") == "task_takeover_during_road_closure":
            resolved = (
                action.get("selected_vehicle_id")
                    != action.get("failed_vehicle_id")
                and bool(closed_edge)
                and closed_edge not in route.get("replanned_edge_ids", [])
                and all(action.get("constraint_results", {}).values())
                and task_status == "completed"
            )
        else:
            resolved = (
                bool(closed_edge)
                and closed_edge not in route.get("replanned_edge_ids", [])
                and task_status == "completed"
            )
        components["scenario_resolution"].update({
            "value": 1.0 if resolved else -1.0, "availability": "available",
            "source": "ordered_compound_road_fault_terminal_result",
        })
    elif scenario_key == "s01":
        components["scenario_resolution"].update({
            "value": 1.0 if task_status == "completed" else -1.0,
            "availability": "available",
            "source": "normal_dispatch_terminal_result",
        })

    original_vehicle = action.get("original_vehicle_id")
    if original_vehicle:
        components["switch_cost"].update({
            "value": -1.0 if original_vehicle != action.get("selected_vehicle_id") else 0.0,
            "availability": "available",
        })
    route = action.get("route", {})
    original_distance = route.get("original_distance_m")
    replanned_distance = route.get("replanned_distance_m")
    maximum_ratio = result.get("maximum_admitted_detour_ratio")
    if (original_distance is not None and replanned_distance is not None
            and maximum_ratio is not None and float(original_distance) > 0
            and float(maximum_ratio) > 1):
        ratio = float(replanned_distance) / float(original_distance)
        normalized_extra = min(
            1.0, max(0.0, ratio - 1.0) / (float(maximum_ratio) - 1.0)
        )
        components["detour_cost"].update({
            "value": -normalized_extra,
            "availability": "available",
            "raw_detour_ratio": ratio,
            "normalization_limit_ratio": float(maximum_ratio),
        })

    weighted = []
    for name, component in components.items():
        value = component.get("value")
        weight = float(reward_config["weights"].get(name, 0.0))
        component["weight"] = weight
        if value is not None and weight > 0:
            weighted.append((weight, float(value)))
    weight_sum = sum(weight for weight, _ in weighted)
    value = (
        round(sum(weight * item for weight, item in weighted) / weight_sum, 6)
        if weight_sum else None
    )
    return {
        "value": value,
        "status": (
            "STRUCTURAL_TERMINAL_REWARD_AVAILABLE"
            if value is not None else "NOT_AVAILABLE_NO_OBSERVED_COMPONENTS"
        ),
        "reward_version": reward_config["reward_version"],
        "weight_status": reward_config["weight_status"],
        "normalization": reward_config["normalization"],
        "components": components,
    }


_LEVEL_GUIDANCE = {
    "blue": {
        "impacts": [
            "当前指标未触发黄色阈值，常规任务可继续执行。",
        ],
        "prevention": [
            "保持固定监测与移动巡检的常规采样频率。",
            "持续保存观测与决策记录，作为趋势对比基线。",
        ],
    },
    "yellow": {
        "impacts": [
            "边坡指标出现上升，风险区附近作业需提高关注等级。",
            "若趋势持续上升，普通巡检和道路通行可能受到影响。",
        ],
        "prevention": [
            "提高固定监测与移动装备的复测频率。",
            "检查通信、相机和定位状态，提前准备融合复核装备。",
        ],
    },
    "orange": {
        "impacts": [
            "边坡变形达到橙色演示阈值，风险区作业安全裕度下降。",
            "风险区及其相邻普通任务可能需要降级或延后。",
        ],
        "prevention": [
            "派出具备相机与激光雷达能力的装备进行融合复核。",
            "限制无关装备接近风险区，并准备道路管控资源。",
            "复核结果未经确认前，不解除风险关注状态。",
        ],
    },
    "red": {
        "impacts": [
            "边坡指标达到红色演示阈值，风险区通行与作业受到直接影响。",
            "人员和装备继续进入风险区可能扩大暴露风险。",
        ],
        "prevention": [
            "立即启用策略层道路限制，阻止无关任务进入风险区。",
            "派发道路管控、融合复核和应急响应任务。",
            "保持应急装备待命，等待人工确认后再调整限制。",
        ],
    },
}


def build_risk_guidance(
    assessment: RiskAssessment,
    risk_scenario: RiskScenario,
    resolved: ResolvedScenario,
) -> Dict[str, Any]:
    """Translate a transparent risk result into auditable guidance."""

    base = _LEVEL_GUIDANCE[assessment.level]
    planned_actions = []
    for action in actions_for_assessment(risk_scenario, assessment):
        planned_actions.append(
            {
                "task_type": action.task_type,
                "zone_id": action.zone_id,
                "priority": action.task_priority,
                "required_capabilities": list(
                    action.required_capabilities
                ),
                "reason": "{}风险触发预配置动作".format(
                    assessment.level
                ),
            }
        )
    restriction = risk_scenario.restriction
    restriction_planned = bool(
        restriction
        and assessment.level in restriction.trigger_levels
    )
    environment_notes = _environment_notes(resolved.environment)
    return {
        "guidance_id": "guidance-{}".format(
            assessment.assessment_id
        ),
        "assessment_id": assessment.assessment_id,
        "tick": assessment.tick,
        "zone_id": assessment.zone_id,
        "risk_level": assessment.level,
        "trend": assessment.trend,
        "impacts": list(base["impacts"]),
        "prevention_measures": list(base["prevention"]),
        "environment_considerations": environment_notes,
        "planned_actions": planned_actions,
        "road_restriction_planned": restriction_planned,
        "decision_basis": {
            "model_version": assessment.model_version,
            "matched_rules": list(assessment.reasons),
            "metrics": dict(assessment.metrics),
            "synthetic_data": assessment.synthetic,
            "confidence_statement": (
                "透明阈值规则命中结果，不是统计概率或真实矿山安全结论"
            ),
        },
        "responsible_agents": [
            "perception_agent",
            "risk_assessment_agent",
            "scheduling_agent",
            "safety_execution_agent",
        ],
        "guidance_version": GUIDANCE_VERSION,
        "human_confirmation_required_for_real_mine": True,
    }


def build_assignment_decisions(
    assignments: Sequence[Assignment],
    context: str,
    tick: Optional[int],
    scenario_seed: int,
) -> List[Dict[str, Any]]:
    """Create explicit multi-agent hand-off records for assignments."""

    return [
        {
            "decision_id": "{}-{}-{}".format(
                context, item.task_id, item.vehicle_id
            ),
            "tick": tick,
            "context": context,
            "state": {
                "task_id": item.task_id,
                "zone_id": item.zone_id,
                "scenario_seed": scenario_seed,
            },
            "perception_agent_output": (
                "车辆状态、位置和能力清单已进入调度状态"
            ),
            "risk_agent_output": (
                "风险任务优先级已计入"
                if context == "risk_response"
                else "按当前场景任务优先级处理"
            ),
            "scheduler_agent_action": {
                "assigned_vehicle_id": item.vehicle_id,
                "score": item.score,
                "reason": item.reason,
                "policy_version": POLICY_VERSION,
            },
            "execution_agent_command": "dispatch_task",
            "learning_mode": "record_only_no_online_update",
        }
        for item in assignments
    ]


def load_imitation_memory(
    artifacts_root: Path,
    scenario_id: str,
) -> Tuple[Dict[Tuple[str, str], float], Dict[str, Any]]:
    """Learn a bounded preference from prior successful task experiences."""

    aggregates: Dict[Tuple[str, str], Dict[str, float]] = {}
    source_files = 0
    root = Path(artifacts_root)
    if root.exists():
        for path in root.glob("*/experience_dataset.jsonl"):
            loaded_from_file = False
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        item = json.loads(line)
                        if (
                            item.get("scenario", {}).get(
                                "scenario_id"
                            )
                            != scenario_id
                            or not item.get(
                                "eligible_for_imitation", False
                            )
                        ):
                            continue
                        task_type = str(
                            item.get("state", {}).get(
                                "task_type", ""
                            )
                        )
                        vehicle_id = str(
                            item.get("action", {}).get(
                                "assigned_vehicle_id", ""
                            )
                        )
                        if not task_type or not vehicle_id:
                            continue
                        key = (task_type, vehicle_id)
                        aggregate = aggregates.setdefault(
                            key, {"count": 0.0, "reward_sum": 0.0}
                        )
                        aggregate["count"] += 1.0
                        aggregate["reward_sum"] += float(
                            item.get("score", {}).get(
                                "reward", 0.0
                            )
                        )
                        loaded_from_file = True
            except (OSError, ValueError, TypeError):
                continue
            if loaded_from_file:
                source_files += 1

    preferences = {}
    entries = []
    for (task_type, vehicle_id), aggregate in sorted(
        aggregates.items()
    ):
        count = int(aggregate["count"])
        mean_reward = aggregate["reward_sum"] / float(count)
        evidence_factor = min(count, 5) / 5.0
        bonus = max(
            0.0,
            min(
                MAX_IMITATION_BONUS_M,
                mean_reward
                * evidence_factor
                * MAX_IMITATION_BONUS_M,
            ),
        )
        preferences[(task_type, vehicle_id)] = bonus
        entries.append(
            {
                "task_type": task_type,
                "vehicle_id": vehicle_id,
                "successful_experience_count": count,
                "mean_reward": round(mean_reward, 4),
                "score_bonus_m": round(bonus, 4),
            }
        )
    return preferences, {
        "memory_version": "bounded-imitation-memory-v1",
        "scenario_id": scenario_id,
        "source_file_count": source_files,
        "successful_experience_count": sum(
            int(item["successful_experience_count"])
            for item in entries
        ),
        "preference_count": len(entries),
        "preferences": entries,
        "maximum_score_bonus_m": MAX_IMITATION_BONUS_M,
        "safety_constraints_overridable": False,
        "risk_thresholds_self_modified": False,
    }


def build_experience_dataset(
    summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Convert final task outcomes into offline-learning records."""

    assessment_by_id = {
        item.get("assessment_id"): item
        for item in summary.get("risk_assessments", [])
    }
    records = []
    for task in summary.get("tasks", []):
        source_id = task.get("source_event_id")
        assessment = assessment_by_id.get(source_id)
        status = str(task.get("status", "unknown"))
        # Experience reward definition
        #
        # CARLA真实执行:
        #   completed  -> 成功完成任务
        #   cancelled  -> 任务取消
        #   timed_out  -> 超时失败
        #
        # Mock模式:
        #   当前阶段只验证Agent调度能力，
        #   assigned表示调度器成功完成任务分配，
        #   因此作为有效经验。


        if status == "completed":

            reward = 1.0


        elif status == "cancelled":

            reward = -0.25


        elif status == "timed_out":

            reward = -1.0


        elif (
            summary.get("mode") == "mock"
            and status == "assigned"
        ):

            # Mock调度验证成功经验
            reward = 1.0


        else:

            reward = 0.0
        records.append(
            {
                "schema_version": EXPERIENCE_SCHEMA_VERSION,
                "experience_id": "{}-{}".format(
                    summary.get("run_id", "run"),
                    task.get("task_id", "task"),
                ),
                "scenario": {
                    "scenario_id": summary.get("scenario_id"),
                    "seed": summary.get("scenario_seed"),
                    "mode": summary.get("scenario_mode"),
                    "simulator_mode": summary.get("mode"),
                },
                "state": {
                    "zone_id": task.get("zone_id"),
                    "task_type": task.get("task_type"),
                    "priority": task.get("priority"),
                    "required_capabilities": task.get(
                        "required_capabilities", []
                    ),
                    "risk_level": (
                        assessment.get("level")
                        if assessment
                        else "not_risk_triggered"
                    ),
                    "risk_metrics": (
                        assessment.get("metrics", {})
                        if assessment
                        else {}
                    ),
                },
                "action": {
                    "assigned_vehicle_id": task.get(
                        "assigned_vehicle_id"
                    ),
                    "policy_version": POLICY_VERSION,
                },
                "result": {
                    "status": status,
                    "attempt_count": task.get("attempt_count"),
                    "started_tick": task.get("started_tick"),
                    "completed_tick": task.get("completed_tick"),
                    "status_reason": task.get("status_reason"),
                },
                "score": {
                    "reward": reward,
                    "formula": (
                        "completed=1.0,cancelled=-0.25,"
                        "timed_out=-1.0,"
                        "mock_assigned=1.0,otherwise=0.0"
                    ),
                },
                "eligible_for_imitation": (
                    status == "completed"
                    or (
                        summary.get("mode") == "mock"
                        and status == "assigned"
                    )
                ),
                "learning_status": (
                    "offline_dataset_only_no_model_trained"
                ),
            }
        )
    return records


def build_structural_transition_dataset(
    result: Dict[str, Any],
    reward_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build conservative decision transitions from one structural run.

    Structural Mock runs have no trustworthy dense reward or immediate
    simulator next-state.  Reward therefore remains unavailable and the
    recorded next state is explicitly the terminal structural snapshot.
    """
    scenario_key = str(result.get("scenario_key") or "unknown")
    task_by_id = {
        str(item.get("task_id")): item
        for item in result.get("tasks", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    initial_assignments = {
        str(item.get("task_id")): item
        for item in result.get("initial_assignments", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    policy_comparison = result.get("policy_comparison", {})
    if not isinstance(policy_comparison, dict):
        policy_comparison = {}
    map_context = result.get("map_context", {})
    if not isinstance(map_context, dict):
        map_context = {}

    shadow_by_task = {
        str(item.get("task_id")): item
        for item in policy_comparison.get("comparisons", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    safety_shield = policy_comparison.get("safety_shield", {})
    if not isinstance(safety_shield, dict):
        safety_shield = {}
    safety_by_task = {
        str(item.get("task_id")): item
        for item in safety_shield.get("reviews", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    decisions = []
    if scenario_key in ("s01", "s02"):
        rankings = result.get("candidate_rankings", {})
        if not isinstance(rankings, dict):
            rankings = {}
        for assignment in result.get("assignments", []):
            if not isinstance(assignment, dict) or not assignment.get("task_id"):
                continue
            task_id = str(assignment["task_id"])
            original = initial_assignments.get(task_id, {})
            decisions.append({
                "task_id": task_id,
                "action_type": (
                    "task_reassignment_after_vehicle_failure"
                    if scenario_key == "s02" else "task_assignment"
                ),
                "selected_vehicle_id": assignment.get("vehicle_id"),
                "original_vehicle_id": original.get("vehicle_id"),
                "score": assignment.get("score"),
                "reason": assignment.get("reason"),
                "candidate_evaluations": rankings.get(task_id, []),
                "policy_version": (
                    policy_comparison.get("executed_policy") or POLICY_VERSION
                ),
                "shadow_policy_evaluation": shadow_by_task.get(task_id),
            })
    elif scenario_key == "s03":
        for item in result.get("equipment_decisions", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            decisions.append({
                "task_id": str(item["task_id"]),
                "action_type": item.get("action_type"),
                "selected_vehicle_id": item.get("vehicle_id"),
                "original_vehicle_id": item.get("vehicle_id"),
                "score": item.get("estimated_task_delay_s"),
                "reason": "failed_equipment_excluded_and_alternative_reachable",
                "candidate_evaluations": [],
                "constraint_results": item.get("constraint_results", {}),
                "policy_version": item.get("policy_version"),
                "route": {
                    "failed_work_point_id": item.get("failed_work_point_id"),
                    "alternative_work_point_id": item.get(
                        "alternative_work_point_id"),
                    "original_edge_ids": item.get("original_edge_ids", []),
                    "replanned_edge_ids": item.get("selected_edge_ids", []),
                    "original_distance_m": item.get("original_distance_m"),
                    "replanned_distance_m": item.get("alternative_distance_m"),
                },
                "timing": {
                    "work_point_switch_delay_s": item.get(
                        "work_point_switch_delay_s"),
                    "estimated_travel_time_change_s": item.get(
                        "estimated_travel_time_change_s"),
                    "estimated_task_delay_s": item.get("estimated_task_delay_s"),
                    "measurement_status": item.get("measurement_status"),
                },
            })
    elif scenario_key == "s04":
        for item in result.get("blast_decisions", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            decisions.append({
                "task_id": str(item["task_id"]),
                "action_type": item.get("action_type"),
                "selected_vehicle_id": item.get("vehicle_id"),
                "original_vehicle_id": item.get("original_vehicle_id"),
                "score": item.get("replanned_distance_m"),
                "reason": item.get("reason"),
                "candidate_evaluations": item.get("candidate_evaluations", []),
                "policy_version": item.get("policy_version"),
                "wait_until_tick": item.get("wait_until_tick"),
                "route": {
                    "restricted_edge_id": item.get("restricted_edge_id"),
                    "original_edge_ids": item.get("original_edge_ids", []),
                    "replanned_edge_ids": item.get("replanned_edge_ids", []),
                    "original_distance_m": item.get("original_distance_m"),
                    "replanned_distance_m": item.get("replanned_distance_m"),
                },
                "measurement_status": item.get("measurement_status"),
            })
    elif scenario_key == "s07":
        for change in result.get("route_changes", []):
            if not isinstance(change, dict) or not change.get("task_id"):
                continue
            decisions.append({
                "task_id": str(change["task_id"]),
                "action_type": change.get("action_type"),
                "selected_vehicle_id": change.get("vehicle_id"),
                "original_vehicle_id": change.get("original_vehicle_id"),
                "score": None,
                "reason": "closed_topology_edge_avoided",
                "candidate_evaluations": change.get("candidate_evaluations", []),
                "policy_version": result.get("route_planner_version"),
                "route": {
                    "closed_edge_id": change.get("closed_edge_id"),
                    "original_edge_ids": change.get("original_edge_ids", []),
                    "replanned_edge_ids": change.get("replanned_edge_ids", []),
                    "original_distance_m": change.get("original_distance_m"),
                    "replanned_distance_m": change.get("replanned_distance_m"),
                },
            })
    elif scenario_key == "s05":
        for item in result.get("weather_decisions", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            decisions.append({
                "task_id": str(item["task_id"]),
                "action_type": item.get("action_type"),
                "selected_vehicle_id": item.get("vehicle_id"),
                "original_vehicle_id": item.get("vehicle_id"),
                "score": item.get("selected_eta_s"),
                "reason": "minimum_estimated_safe_travel_time",
                "candidate_evaluations": [],
                "policy_version": item.get("policy_version"),
                "eta": {
                    "baseline_eta_s": item.get("baseline_eta_s"),
                    "restricted_route_eta_s": item.get("restricted_route_eta_s"),
                    "alternative_route_eta_s": item.get("alternative_route_eta_s"),
                    "selected_eta_s": item.get("selected_eta_s"),
                    "estimated_delay_s": item.get("estimated_delay_s"),
                    "source": item.get("eta_source"),
                    "measurement_status": item.get("measurement_status"),
                },
                "route": {
                    "degraded_edge_id": item.get("degraded_edge_id"),
                    "original_edge_ids": item.get("original_edge_ids", []),
                    "selected_edge_ids": item.get("selected_edge_ids", []),
                    "original_distance_m": item.get("original_distance_m"),
                    "replanned_distance_m": item.get("selected_distance_m"),
                },
            })
    elif scenario_key == "s06":
        for item in result.get("traffic_decisions", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            decisions.append({
                "task_id": str(item["task_id"]),
                "action_type": item.get("action_type"),
                "selected_vehicle_id": item.get("vehicle_id"),
                "original_vehicle_id": item.get("vehicle_id"),
                "score": item.get("estimated_wait_s"),
                "reason": "capacity_one_eta_priority_headway_schedule",
                "candidate_evaluations": [],
                "policy_version": item.get("policy_version"),
                "traffic": {
                    "bottleneck_edge_id": item.get("bottleneck_edge_id"),
                    "queue_position": item.get("queue_position"),
                    "estimated_arrival_s": item.get("estimated_arrival_s"),
                    "scheduled_entry_s": item.get("scheduled_entry_s"),
                    "scheduled_exit_s": item.get("scheduled_exit_s"),
                    "estimated_wait_s": item.get("estimated_wait_s"),
                    "minimum_safety_headway_seconds": item.get(
                        "minimum_safety_headway_seconds"),
                    "time_source": item.get("time_source"),
                    "measurement_status": item.get("measurement_status"),
                },
                "route": {
                    "bottleneck_edge_id": item.get("bottleneck_edge_id"),
                },
            })
    elif scenario_key == "s09":
        for change in result.get("route_changes", []):
            if not isinstance(change, dict) or not change.get("task_id"):
                continue
            decisions.append({
                "task_id": str(change["task_id"]),
                "action_type": change.get("action_type"),
                "selected_vehicle_id": change.get("vehicle_id"),
                "original_vehicle_id": change.get("original_vehicle_id"),
                "score": change.get("replanned_distance_m"),
                "reason": "compound_stage_1_closed_edge_avoided",
                "candidate_evaluations": change.get("candidate_evaluations", []),
                "policy_version": result.get("route_planner_version"),
                "compound_stage": 1,
                "route": {
                    "closed_edge_id": change.get("closed_edge_id"),
                    "original_edge_ids": change.get("original_edge_ids", []),
                    "replanned_edge_ids": change.get("replanned_edge_ids", []),
                    "original_distance_m": change.get("original_distance_m"),
                    "replanned_distance_m": change.get("replanned_distance_m"),
                },
            })
        for item in result.get("compound_failure_decisions", []):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            decisions.append({
                "task_id": str(item["task_id"]),
                "action_type": item.get("action_type"),
                "selected_vehicle_id": item.get("selected_vehicle_id"),
                "original_vehicle_id": item.get("failed_vehicle_id"),
                "failed_vehicle_id": item.get("failed_vehicle_id"),
                "score": item.get("route_distance_m"),
                "reason": "compound_stage_2_safe_capable_takeover",
                "candidate_evaluations": item.get("candidate_evaluations", []),
                "constraint_results": item.get("constraint_results", {}),
                "policy_version": item.get("policy_version"),
                "compound_stage": 2,
                "route": {
                    "closed_edge_id": item.get("closed_edge_id"),
                    "original_edge_ids": [],
                    "replanned_edge_ids": item.get("route_edge_ids", []),
                    "original_distance_m": None,
                    "replanned_distance_m": item.get("route_distance_m"),
                },
            })

    source_decision_by_key = {}
    for collection_name in (
        "equipment_decisions", "blast_decisions", "weather_decisions",
        "traffic_decisions", "route_changes", "compound_failure_decisions",
    ):
        for item in result.get(collection_name, []):
            if not isinstance(item, dict):
                continue
            source_decision_by_key.setdefault((
                str(item.get("task_id")), str(item.get("action_type")),
            ), item)

    if scenario_key == "s02":
        state_vehicles = result.get("fault_vehicle_states", [])
        trigger = {
            "event_type": "vehicle_fault",
            "vehicle_id": result.get("failed_vehicle_id"),
            "tick": result.get("failure_tick"),
        }
    elif scenario_key == "s03":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "loading_equipment_failure",
            "equipment": result.get("equipment_event", {}),
            "affected_task_ids": result.get("affected_task_ids", []),
        }
    elif scenario_key == "s04":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "planned_blasting_temporary_control",
            "blast": result.get("blast_event", {}),
            "affected_task_ids": result.get("route_impact_task_ids", []),
        }
    elif scenario_key == "s07":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "road_closure",
            "edge_id": result.get("closed_edge_id"),
            "affected_task_ids": result.get("route_impact_task_ids", []),
        }
    elif scenario_key == "s05":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "extreme_rainfall_road_capacity_degradation",
            "degraded_edge_id": result.get("degraded_edge_id"),
            "affected_task_ids": result.get("route_impact_task_ids", []),
            "weather": result.get("weather_event", {}),
        }
    elif scenario_key == "s06":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "shared_road_capacity_degradation",
            "bottleneck_edge_id": result.get("bottleneck_edge_id"),
            "affected_task_ids": result.get("route_impact_task_ids", []),
            "traffic": result.get("congestion_event", {}),
        }
    elif scenario_key == "s09":
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {
            "event_type": "compound_road_closure_then_vehicle_failure",
            "events": result.get("compound_events", []),
            "affected_task_ids": result.get("compound_affected_task_ids", []),
        }
    else:
        state_vehicles = result.get("initial_vehicle_states", [])
        trigger = {"event_type": "normal_task_dispatch"}

    records = []
    for index, decision in enumerate(decisions):
        task = task_by_id.get(decision["task_id"], {})
        state_task = dict(task)
        if scenario_key == "s01":
            state_task.update({
                "status": "pending", "assigned_vehicle_id": None,
                "completed_tick": None, "status_reason": None,
            })
        elif scenario_key == "s02":
            state_task.update({
                "status": "pending", "assigned_vehicle_id": None,
                "completed_tick": None,
                "status_reason": "released_after_vehicle_fault",
            })
        elif scenario_key == "s03":
            state_task.update({
                "status": "paused",
                "assigned_vehicle_id": decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": "assigned_loading_equipment_unavailable",
            })
        elif scenario_key == "s04":
            state_task.update({
                "status": "assigned",
                "assigned_vehicle_id": decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": "active_route_affected_by_planned_blast_control",
            })
        elif scenario_key == "s07":
            state_task.update({
                "status": "assigned",
                "assigned_vehicle_id": decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": "active_route_affected_by_road_closure",
            })
        elif scenario_key == "s05":
            state_task.update({
                "status": "assigned",
                "assigned_vehicle_id": decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": "active_route_affected_by_extreme_weather",
            })
        elif scenario_key == "s06":
            state_task.update({
                "status": "assigned",
                "assigned_vehicle_id": decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": "active_route_affected_by_shared_road_congestion",
            })
        elif scenario_key == "s09":
            state_task.update({
                "status": "pending" if decision.get("compound_stage") == 2
                    else "assigned",
                "assigned_vehicle_id": None if decision.get("compound_stage") == 2
                    else decision.get("original_vehicle_id"),
                "completed_tick": None,
                "status_reason": (
                    "released_after_vehicle_fault_during_road_closure"
                    if decision.get("compound_stage") == 2
                    else "active_route_affected_by_compound_road_closure"
                ),
            })
        evidence_status = result.get(
            "database_evidence_validation", {}
        ).get("status")
        eligible_for_bc = (
            result.get("closed_loop_status") == "CLOSED_LOOP_PASS"
            and evidence_status == "EVIDENCE_PASS"
        )
        final_world_state = result.get("world_state", {})
        if not isinstance(final_world_state, dict):
            final_world_state = {}
        decision_state = {
            "schema_version": WORLD_STATE_SCHEMA_VERSION,
            "run_id": result.get("run_id"),
            "tick": trigger.get("tick"),
            "vehicles": state_vehicles,
            # Structural results do not preserve every task at every decision.
            # Keep only the affected task instead of presenting terminal task
            # states as if they were observed decision-time state.
            "tasks": [state_task],
            "roads": {},
            "environment": {
                "map_context": dict(map_context),
            },
            "monitoring": {},
            "risk": {},
            "traffic": {},
            "equipment": {},
            "snapshot_kind": "decision_input_structural_snapshot",
            "snapshot_metadata": {
                "fidelity": "structural",
                "completeness": "partial_affected_task_scope",
                "unavailable_domains": [
                    "dense_monitoring", "dense_risk", "physical_traffic",
                    "physical_execution",
                ],
            },
            # Compatibility fields retained for existing dataset consumers.
            "task": state_task,
            "trigger": trigger,
        }
        terminal_world_state = {
            "schema_version": WORLD_STATE_SCHEMA_VERSION,
            "run_id": result.get("run_id"),
            "tick": final_world_state.get("tick"),
            "vehicles": final_world_state.get(
                "vehicles", result.get("final_vehicle_states", [])
            ),
            "tasks": final_world_state.get("tasks", result.get("tasks", [])),
            "roads": final_world_state.get("roads", {}),
            "environment": final_world_state.get("environment", {}),
            "monitoring": final_world_state.get("monitoring", {}),
            "risk": final_world_state.get("risk", {}),
            "traffic": final_world_state.get("traffic", {}),
            "equipment": final_world_state.get("equipment", {}),
            "snapshot_kind": "terminal_structural_snapshot",
            "snapshot_metadata": {
                "fidelity": "structural",
                "completeness": "scenario_terminal_snapshot",
            },
            # Compatibility field retained for existing dataset consumers.
            "task": task,
        }
        decision = dict(decision)
        decision["schema_version"] = DECISION_ACTION_SCHEMA_VERSION
        source_decision = source_decision_by_key.get((
            str(decision.get("task_id")), str(decision.get("action_type")),
        ), {})
        if source_decision.get("constraint_results") is not None:
            decision["constraint_results"] = source_decision[
                "constraint_results"
            ]
        if source_decision.get("safety_review") is not None:
            decision["safety_review"] = source_decision["safety_review"]
        if source_decision.get("route_contract") is not None:
            decision["route_contract"] = source_decision["route_contract"]
        safety_review = safety_by_task.get(str(decision.get("task_id")))
        if safety_review is not None:
            if safety_shield.get("mode") == "execution_gate":
                decision["safety_review"] = safety_review
            else:
                decision["shadow_safety_review"] = safety_review
        record = {
            "schema_version": STRUCTURAL_TRANSITION_SCHEMA_VERSION,
            "experience_id": "{}:decision:{}".format(
                result.get("run_id") or result.get("scenario_id") or "run", index
            ),
            "episode": {
                "run_id": result.get("run_id"),
                "scenario_id": result.get("scenario_id"),
                "scenario_key": scenario_key,
                "seed": result.get("seed"),
                "vehicle_count": result.get("fleet", {}).get("total"),
                "simulation_mode": result.get("mode"),
                "random_mode": result.get("random_mode"),
            },
            "map_context": {
                "map_id": map_context.get("map_id"),
                "resource_version": map_context.get("resource_version"),
                "source": result.get("scenario_source"),
            },
            "state": decision_state,
            "action": decision,
            "result": {
                "schema_version": EXECUTION_FEEDBACK_SCHEMA_VERSION,
                "task_status": task.get("status"),
                "status_reason": task.get("status_reason"),
                "estimated_delay_s": decision.get("eta", {}).get(
                    "estimated_delay_s"
                ),
                "estimated_wait_s": decision.get("traffic", {}).get(
                    "estimated_wait_s"
                ),
                "traffic_control_status_after_recovery": result.get(
                    "traffic_control_status_after_recovery"
                ),
                "equipment_status_after_recovery": result.get(
                    "equipment_status_after_recovery"
                ),
                "blast_zone_status_after_clearance": result.get(
                    "blast_zone_status_after_clearance"
                ),
                "closed_loop_status": result.get("closed_loop_status"),
                "database_evidence_status": evidence_status,
            },
            "next_state": terminal_world_state,
            "reward": None,
            "reward_status": "NOT_AVAILABLE_NO_REWARD_MODEL",
            "reward_detail": {},
            "done": True,
            "safety_override": None,
            "safety_override_status": "NOT_AVAILABLE_NO_SAFETY_SHIELD_TRACE",
            "policy_version": decision.get("policy_version"),
            "data_quality": {
                "transition_granularity": "decision_to_terminal_structural",
                "carla_physical_execution": "NOT_AVAILABLE",
                "real_energy_measurement": "NOT_AVAILABLE",
                "real_mine_data": False,
                "eligible_for_behavior_cloning_preparation": eligible_for_bc,
                "eligible_for_ppo_training": False,
            },
        }
        reward = _evaluate_structural_reward(record, result, reward_config)
        record["reward"] = reward["value"]
        record["reward_status"] = reward["status"]
        record["reward_detail"] = reward
        records.append(record)
    return records


def write_structural_transition_dataset(
    results: Sequence[Dict[str, Any]],
    datasets_root: Path,
    dataset_id: str,
    reward_config: Optional[Dict[str, Any]] = None,
    expected_scenarios: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Write one immutable multi-run JSONL dataset plus its manifest."""
    output_dir = (
        Path(datasets_root) / STRUCTURAL_TRANSITION_SCHEMA_VERSION / dataset_id
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    for result in results:
        records.extend(build_structural_transition_dataset(result, reward_config))
    transitions_path = output_dir / "transitions.jsonl"
    with transitions_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    scenario_counts: Dict[str, int] = {}
    scenario_rewards: Dict[str, List[float]] = {}
    for record in records:
        key = str(record["episode"].get("scenario_key") or "unknown")
        scenario_counts[key] = scenario_counts.get(key, 0) + 1
        if record.get("reward") is not None:
            scenario_rewards.setdefault(key, []).append(float(record["reward"]))
    reward_values = [
        float(record["reward"]) for record in records
        if record.get("reward") is not None
    ]
    quality = build_structural_dataset_quality(
        records, results, expected_scenarios=expected_scenarios
    )
    manifest = {
        "schema_version": STRUCTURAL_TRANSITION_SCHEMA_VERSION,
        "transition_contract": {
            "state": WORLD_STATE_SCHEMA_VERSION,
            "action": DECISION_ACTION_SCHEMA_VERSION,
            "feedback": EXECUTION_FEEDBACK_SCHEMA_VERSION,
            "next_state": WORLD_STATE_SCHEMA_VERSION,
        },
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "source_run_count": len(results),
        "source_run_ids": [item.get("run_id") for item in results],
        "scenario_record_counts": scenario_counts,
        "transitions_path": str(transitions_path),
        "reward_status": (
            "STRUCTURAL_TERMINAL_REWARD_AVAILABLE"
            if reward_config is not None else "NOT_AVAILABLE_NO_REWARD_MODEL"
        ),
        "reward_version": (
            reward_config.get("reward_version") if reward_config else None
        ),
        "reward_weight_status": (
            reward_config.get("weight_status") if reward_config else None
        ),
        "reward_summary": {
            "available_record_count": len(reward_values),
            "unavailable_record_count": len(records) - len(reward_values),
            "minimum": min(reward_values) if reward_values else None,
            "maximum": max(reward_values) if reward_values else None,
            "mean": (
                round(sum(reward_values) / len(reward_values), 6)
                if reward_values else None
            ),
            "scenario_means": {
                key: round(sum(values) / len(values), 6)
                for key, values in sorted(scenario_rewards.items())
            },
        },
        "dataset_quality": quality,
        "intended_use": "behavior_cloning_preparation_and_offline_analysis",
        "not_valid_for": [
            "ppo_training", "carla_physical_performance_claims",
            "real_mine_safety_claims",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_structural_dataset_quality(
    records: Sequence[Dict[str, Any]],
    results: Sequence[Dict[str, Any]],
    expected_scenarios: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Describe dataset coverage and integrity without inventing samples."""
    records = list(records)
    results = list(results)
    expected = sorted(set(str(item) for item in (expected_scenarios or ())))
    observed = sorted(set(
        str(item.get("episode", {}).get("scenario_key") or "unknown")
        for item in records
    ))

    def counts(values):
        output: Dict[str, int] = {}
        for value in values:
            key = str(value if value is not None else "NOT_AVAILABLE")
            output[key] = output.get(key, 0) + 1
        return dict(sorted(output.items()))

    def percentile(values, fraction):
        if not values:
            return None
        ordered = sorted(float(item) for item in values)
        if len(ordered) == 1:
            return ordered[0]
        position = fraction * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        remainder = position - lower
        return round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * remainder, 6
        )

    critical_fields = {
        "experience_id": lambda item: item.get("experience_id"),
        "state_schema_version": lambda item: item.get("state", {}).get(
            "schema_version"
        ),
        "action_schema_version": lambda item: item.get("action", {}).get(
            "schema_version"
        ),
        "feedback_schema_version": lambda item: item.get("result", {}).get(
            "schema_version"
        ),
        "next_state_schema_version": lambda item: item.get(
            "next_state", {}
        ).get("schema_version"),
        "scenario_key": lambda item: item.get("episode", {}).get("scenario_key"),
        "seed": lambda item: item.get("episode", {}).get("seed"),
        "map_id": lambda item: item.get("map_context", {}).get("map_id"),
        "task_id": lambda item: item.get("action", {}).get("task_id"),
        "action_type": lambda item: item.get("action", {}).get("action_type"),
        "selected_vehicle_id": lambda item: item.get("action", {}).get(
            "selected_vehicle_id"
        ),
        "task_status": lambda item: item.get("result", {}).get("task_status"),
        "closed_loop_status": lambda item: item.get("result", {}).get(
            "closed_loop_status"
        ),
        "database_evidence_status": lambda item: item.get("result", {}).get(
            "database_evidence_status"
        ),
    }
    missing = {
        name: sum(getter(item) is None for item in records)
        for name, getter in critical_fields.items()
    }
    experience_ids = [item.get("experience_id") for item in records]
    duplicate_count = len(experience_ids) - len(set(experience_ids))
    rewards = [
        float(item["reward"]) for item in records if item.get("reward") is not None
    ]
    failed_runs = [
        {
            "run_id": item.get("run_id"),
            "scenario_key": item.get("scenario_key"),
            "seed": item.get("seed"),
            "error": item.get("error"),
        }
        for item in results if item.get("status") != "PASS"
    ]
    checks = [
        {
            "check": "records_present", "passed": bool(records),
            "detail": len(records),
        },
        {
            "check": "expected_scenarios_present",
            "passed": not set(expected).difference(observed),
            "detail": sorted(set(expected).difference(observed)),
        },
        {
            "check": "experience_ids_unique", "passed": duplicate_count == 0,
            "detail": duplicate_count,
        },
        {
            "check": "critical_fields_complete",
            "passed": not any(missing.values()), "detail": missing,
        },
        {
            "check": "source_runs_passed", "passed": not failed_runs,
            "detail": len(failed_runs),
        },
        {
            "check": "closed_loop_evidence_passed",
            "passed": bool(records) and all(
                item.get("result", {}).get("closed_loop_status")
                == "CLOSED_LOOP_PASS"
                and item.get("result", {}).get("database_evidence_status")
                == "EVIDENCE_PASS"
                for item in records
            ),
            "detail": counts(
                item.get("result", {}).get("database_evidence_status")
                for item in records
            ),
        },
    ]
    return {
        "status": (
            "DATASET_QUALITY_PASS"
            if all(item["passed"] for item in checks) else "DATASET_QUALITY_WARN"
        ),
        "checks": checks,
        "expected_scenarios": expected,
        "observed_scenarios": observed,
        "missing_expected_scenarios": sorted(set(expected).difference(observed)),
        "unique_seed_count": len(set(
            item.get("episode", {}).get("seed") for item in records
        )),
        "scenario_seed_counts": {
            scenario: len(set(
                item.get("episode", {}).get("seed") for item in records
                if item.get("episode", {}).get("scenario_key") == scenario
            )) for scenario in observed
        },
        "action_type_distribution": counts(
            item.get("action", {}).get("action_type") for item in records
        ),
        "selected_vehicle_distribution": counts(
            item.get("action", {}).get("selected_vehicle_id") for item in records
        ),
        "failed_vehicle_distribution": counts(
            item.get("failed_vehicle_id") for item in results
            if item.get("failed_vehicle_id") is not None
        ),
        "closed_edge_distribution": counts(
            item.get("closed_edge_id") for item in results
            if item.get("closed_edge_id") is not None
        ),
        "source_failed_run_count": len(failed_runs),
        "source_failed_runs": failed_runs,
        "duplicate_experience_id_count": duplicate_count,
        "missing_critical_field_counts": missing,
        "behavior_cloning_eligible_count": sum(
            bool(item.get("data_quality", {}).get(
                "eligible_for_behavior_cloning_preparation"
            )) for item in records
        ),
        "reward_distribution": {
            "count": len(rewards),
            "minimum": min(rewards) if rewards else None,
            "p25": percentile(rewards, 0.25),
            "median": percentile(rewards, 0.5),
            "p75": percentile(rewards, 0.75),
            "maximum": max(rewards) if rewards else None,
            "mean": round(sum(rewards) / len(rewards), 6) if rewards else None,
        },
        "limitations": [
            "structural_mock_decision_to_terminal_samples_only",
            "no_carla_physical_trajectory",
            "no_real_energy_or_collision_measurements",
            "failed_runs_are_reported_not_fabricated_as_transitions",
        ],
    }


def aggregate_structural_transition_datasets(
    datasets_root: Path,
    version_id: str,
    split_seed: int = 202616,
    validation_ratio: float = 0.15,
    test_ratio: float = 0.15,
) -> Dict[str, Any]:
    """Create an immutable, seed-isolated BC preparation dataset version."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$", str(version_id)):
        raise ValueError("dataset version must use 1-80 safe filename characters")
    if validation_ratio <= 0 or test_ratio <= 0 or validation_ratio + test_ratio >= 1:
        raise ValueError("validation/test ratios must be positive and sum to less than 1")
    schema_root = Path(datasets_root) / STRUCTURAL_TRANSITION_SCHEMA_VERSION
    source_manifests = []
    skipped_sources = []
    records = []
    for manifest_path in sorted(schema_root.glob("structural-batch-*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reasons = []
        if manifest.get("schema_version") != STRUCTURAL_TRANSITION_SCHEMA_VERSION:
            reasons.append("schema_version_mismatch")
        if manifest.get("reward_version") != "structural-terminal-reward-v1":
            reasons.append("reward_version_mismatch_or_missing")
        if manifest.get("dataset_quality", {}).get("status") != "DATASET_QUALITY_PASS":
            reasons.append("dataset_quality_not_passed")
        transitions_path = manifest_path.parent / "transitions.jsonl"
        if not transitions_path.is_file():
            reasons.append("transitions_file_missing")
        if reasons:
            skipped_sources.append({
                "manifest_path": str(manifest_path), "reasons": reasons,
            })
            continue
        source_manifests.append(str(manifest_path))
        with transitions_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                item["dataset_provenance"] = {
                    "source_dataset_id": manifest.get("dataset_id"),
                    "source_line_number": line_number,
                }
                records.append(item)
    if not source_manifests:
        raise ValueError("no quality-passed structural batch dataset is available")

    def semantic_key(item):
        action = item.get("action", {})
        return json.dumps({
            "scenario_key": item.get("episode", {}).get("scenario_key"),
            "seed": item.get("episode", {}).get("seed"),
            "map_id": item.get("map_context", {}).get("map_id"),
            "resource_version": item.get("map_context", {}).get("resource_version"),
            "task_id": action.get("task_id"),
            "action_type": action.get("action_type"),
            "selected_vehicle_id": action.get("selected_vehicle_id"),
            "original_vehicle_id": action.get("original_vehicle_id"),
            "route": action.get("route"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    deduplicated = []
    signatures = set()
    duplicate_count = 0
    excluded_ineligible_count = 0
    for item in records:
        if not item.get("data_quality", {}).get(
            "eligible_for_behavior_cloning_preparation", False
        ):
            excluded_ineligible_count += 1
            continue
        signature = semantic_key(item)
        if signature in signatures:
            duplicate_count += 1
            continue
        signatures.add(signature)
        item["semantic_sample_id"] = "semantic:{}".format(len(signatures))
        deduplicated.append(item)
    seeds = sorted(set(
        int(item["episode"]["seed"]) for item in deduplicated
        if item.get("episode", {}).get("seed") is not None
    ))
    if len(seeds) < 3:
        raise ValueError("at least three unique seeds are required for train/validation/test")
    Random(int(split_seed)).shuffle(seeds)
    test_count = max(1, int(round(len(seeds) * float(test_ratio))))
    validation_count = max(1, int(round(len(seeds) * float(validation_ratio))))
    while test_count + validation_count >= len(seeds):
        if test_count >= validation_count and test_count > 1:
            test_count -= 1
        elif validation_count > 1:
            validation_count -= 1
        else:
            break
    test_seeds = set(seeds[:test_count])
    validation_seeds = set(seeds[test_count:test_count + validation_count])
    train_seeds = set(seeds[test_count + validation_count:])
    split_seeds = {
        "train": train_seeds,
        "validation": validation_seeds,
        "test": test_seeds,
    }
    actions_by_seed: Dict[int, set] = {}
    scenarios_by_seed: Dict[int, set] = {}
    for item in deduplicated:
        seed = int(item["episode"]["seed"])
        actions_by_seed.setdefault(seed, set()).add(
            str(item.get("action", {}).get("action_type"))
        )
        scenarios_by_seed.setdefault(seed, set()).add(
            str(item.get("episode", {}).get("scenario_key"))
        )
    all_actions = set().union(*actions_by_seed.values())
    all_scenarios = set().union(*scenarios_by_seed.values())

    def split_missing(seed_groups):
        output = {}
        for name, values in seed_groups.items():
            actions = set().union(*(actions_by_seed[seed] for seed in values))
            scenarios = set().union(*(scenarios_by_seed[seed] for seed in values))
            output[name] = {
                "actions": sorted(all_actions.difference(actions)),
                "scenarios": sorted(all_scenarios.difference(scenarios)),
            }
        return output

    def coverage_score(seed_groups):
        return sum(
            len(item["actions"]) + len(item["scenarios"])
            for item in split_missing(seed_groups).values()
        )

    # Preserve split sizes while deterministically swapping whole seed groups
    # to improve action/scenario coverage.  No record-level split is allowed.
    current_score = coverage_score(split_seeds)
    while current_score:
        best = None
        names = ("train", "validation", "test")
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1:]:
                for left_seed in sorted(split_seeds[left_name]):
                    for right_seed in sorted(split_seeds[right_name]):
                        candidate = {
                            name: set(values) for name, values in split_seeds.items()
                        }
                        candidate[left_name].remove(left_seed)
                        candidate[left_name].add(right_seed)
                        candidate[right_name].remove(right_seed)
                        candidate[right_name].add(left_seed)
                        score = coverage_score(candidate)
                        rank = (score, left_name, right_name, left_seed, right_seed)
                        if score < current_score and (best is None or rank < best[0]):
                            best = (rank, candidate)
        if best is None:
            break
        split_seeds = best[1]
        current_score = best[0][0]
    train_seeds = split_seeds["train"]
    validation_seeds = split_seeds["validation"]
    test_seeds = split_seeds["test"]
    coverage_missing = split_missing(split_seeds)
    split_records = {name: [] for name in split_seeds}
    for item in deduplicated:
        seed = int(item["episode"]["seed"])
        destination = next(
            name for name, values in split_seeds.items() if seed in values
        )
        split_records[destination].append(item)

    output_dir = schema_root / "versions" / str(version_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    split_files = {}
    global_split_files = {}
    global_split_records = {}
    for name in ("train", "validation", "test"):
        path = output_dir / "{}.jsonl".format(name)
        with path.open("w", encoding="utf-8") as handle:
            for item in split_records[name]:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        split_files[name] = str(path)
        global_records = build_global_assignment_records(split_records[name])
        global_path = output_dir / "global_{}.jsonl".format(name)
        with global_path.open("w", encoding="utf-8") as handle:
            for item in global_records:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        global_split_files[name] = str(global_path)
        global_split_records[name] = global_records

    def distribution(items, getter):
        output: Dict[str, int] = {}
        for item in items:
            key = str(getter(item))
            output[key] = output.get(key, 0) + 1
        return dict(sorted(output.items()))

    seed_overlap = {
        "train_validation": sorted(train_seeds.intersection(validation_seeds)),
        "train_test": sorted(train_seeds.intersection(test_seeds)),
        "validation_test": sorted(validation_seeds.intersection(test_seeds)),
    }
    manifest = {
        "dataset_version": str(version_id),
        "schema_version": STRUCTURAL_TRANSITION_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "DATASET_VERSION_INVALID"
            if not train_seeds or not validation_seeds or not test_seeds
            or any(seed_overlap.values())
            else "DATASET_VERSION_READY_WITH_COVERAGE_WARNINGS"
            if any(
                item["actions"] or item["scenarios"]
                for item in coverage_missing.values()
            )
            else "DATASET_VERSION_READY"
        ),
        "source_manifest_count": len(source_manifests),
        "source_manifests": source_manifests,
        "skipped_sources": skipped_sources,
        "raw_record_count": len(records),
        "semantic_duplicate_count": duplicate_count,
        "excluded_ineligible_count": excluded_ineligible_count,
        "record_count": len(deduplicated),
        "unique_seed_count": len(seeds),
        "split_policy": {
            "method": "deterministic_seed_group_split",
            "split_seed": int(split_seed),
            "requested_validation_ratio": float(validation_ratio),
            "requested_test_ratio": float(test_ratio),
            "same_seed_cross_split_allowed": False,
        },
        "seed_overlap": seed_overlap,
        "split_coverage_missing": coverage_missing,
        "splits": {
            name: {
                "path": split_files[name],
                "record_count": len(split_records[name]),
                "seed_count": len(split_seeds[name]),
                "seeds": sorted(split_seeds[name]),
                "scenario_distribution": distribution(
                    split_records[name],
                    lambda item: item.get("episode", {}).get("scenario_key"),
                ),
                "action_distribution": distribution(
                    split_records[name],
                    lambda item: item.get("action", {}).get("action_type"),
                ),
            } for name in ("train", "validation", "test")
        },
        "global_assignment_view": {
            "schema_version": GLOBAL_ASSIGNMENT_SCHEMA_VERSION,
            "source_scenario": "s01",
            "decision_scope": "fleet_task_bipartite_assignment",
            "splits": {
                name: {
                    "path": global_split_files[name],
                    "episode_count": len(global_split_records[name]),
                    "seed_count": len(set(
                        item["episode"]["seed"] for item in global_split_records[name]
                    )),
                    "task_decision_count": sum(
                        len(item["action"]["assignments"])
                        for item in global_split_records[name]
                    ),
                } for name in ("train", "validation", "test")
            },
            "limitations": [
                "s01_global_assignment_only",
                "structural_mock_teacher_actions",
                "no_carla_physical_execution",
            ],
        },
        "map_resource_versions": sorted(set(
            "{}:{}".format(
                item.get("map_context", {}).get("map_id"),
                item.get("map_context", {}).get("resource_version"),
            ) for item in deduplicated
        )),
        "policy_versions": sorted(set(
            str(item.get("policy_version")) for item in deduplicated
        )),
        "reward_versions": sorted(set(
            str(item.get("reward_detail", {}).get("reward_version"))
            for item in deduplicated
        )),
        "intended_use": "behavior_cloning_train_validation_test_preparation",
        "not_valid_for": [
            "ppo_training", "carla_physical_performance_claims",
            "real_mine_safety_claims",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_global_assignment_records(
    transition_records: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Group S01 task transitions into episode-level assignment examples."""
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for item in transition_records:
        episode = item.get("episode", {})
        action = item.get("action", {})
        if (episode.get("scenario_key") != "s01"
                or action.get("action_type") != "task_assignment"
                or episode.get("seed") is None):
            continue
        key = (str(episode.get("scenario_id")), int(episode["seed"]))
        groups.setdefault(key, []).append(item)
    records = []
    for (scenario_id, seed), items in sorted(groups.items()):
        items.sort(key=lambda item: str(item.get("action", {}).get("task_id")))
        assignments = []
        candidate_matrix = {}
        tasks = []
        valid = len(items) >= 2
        for item in items:
            action = item["action"]
            task_id = str(action.get("task_id"))
            candidates = (
                (action.get("shadow_policy_evaluation") or {})
                .get("v1_candidate_ranking", [])
            )
            selected = action.get("selected_vehicle_id")
            feasible_ids = {
                candidate.get("vehicle_id") for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("feasible")
            }
            valid = valid and selected in feasible_ids
            assignments.append({
                "task_id": task_id,
                "vehicle_id": selected,
                "teacher_policy_version": action.get("policy_version"),
            })
            candidate_matrix[task_id] = candidates
            tasks.append(item.get("state", {}).get("task", {}))
        selected_ids = [item["vehicle_id"] for item in assignments]
        valid = valid and len(selected_ids) == len(set(selected_ids))
        first = items[0]
        records.append({
            "schema_version": GLOBAL_ASSIGNMENT_SCHEMA_VERSION,
            "global_experience_id": "{}:{}".format(scenario_id, seed),
            "episode": {
                "scenario_id": scenario_id,
                "scenario_key": "s01",
                "seed": seed,
                "vehicle_count": first.get("episode", {}).get("vehicle_count"),
                "simulation_mode": first.get("episode", {}).get("simulation_mode"),
            },
            "map_context": first.get("map_context", {}),
            "state": {
                "vehicles": first.get("state", {}).get("vehicles", []),
                "tasks": tasks,
                "candidate_matrix": candidate_matrix,
            },
            "action": {
                "action_type": "global_unique_task_vehicle_assignment",
                "assignments": assignments,
            },
            "result": {
                "all_tasks_completed": all(
                    item.get("result", {}).get("task_status") == "completed"
                    for item in items
                ),
                "closed_loop_status": first.get("result", {}).get(
                    "closed_loop_status"
                ),
                "database_evidence_status": first.get("result", {}).get(
                    "database_evidence_status"
                ),
            },
            "data_quality": {
                "eligible_for_global_behavior_cloning": bool(valid),
                "teacher_assignment_unique": len(selected_ids) == len(set(selected_ids)),
                "candidate_features_from": "multi-objective-cost-v1-shadow",
                "carla_physical_execution": "NOT_AVAILABLE",
            },
        })
    return records


def build_acceptance_report(
    summary: Dict[str, Any],
    decision_count: int,
    experience_count: int,
) -> Dict[str, Any]:
    """Build a conservative, machine-readable functional acceptance report."""

    mode = str(summary.get("mode", "unknown"))
    tasks = list(summary.get("tasks", []))
    assessments = list(summary.get("risk_assessments", []))
    guidance = list(summary.get("risk_guidance", []))
    checks = []

    def add(
        check_id: str,
        passed: bool,
        evidence: Any,
        required: bool = True,
    ) -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": bool(passed),
                "required": required,
                "evidence": evidence,
            }
        )

    add(
        "scenario_reproducible",
        summary.get("scenario_seed") is not None,
        {"scenario_seed": summary.get("scenario_seed")},
    )
    add(
        "multi_agent_decisions_auditable",
        decision_count > 0,
        {"decision_record_count": decision_count},
    )
    if mode == "mock":
        add(
            "mock_structure_validated",
            summary.get("status") == "PASS",
            {"status": summary.get("status")},
        )
    else:
        add(
            "all_tasks_completed",
            bool(tasks)
            and all(item.get("status") == "completed" for item in tasks),
            {
                "completed": sum(
                    item.get("status") == "completed" for item in tasks
                ),
                "total": len(tasks),
            },
        )
        speeds = [
            float(item.get("speed_mps", 0.0))
            for item in summary.get("vehicle_states", [])
        ]
        add(
            "terminal_vehicles_stopped",
            bool(speeds) and max(speeds) <= 0.05,
            {"max_speed_mps": max(speeds) if speeds else None},
        )
    if summary.get("failure_injected"):
        if mode == "mock":
            add(
                "failure_recovery_decision_generated",
                int(summary.get("reassignment_count", 0)) > 0,
                {
                    "failure_injected": True,
                    "reassignment_count": summary.get(
                        "reassignment_count", 0
                    ),
                },
            )
        else:
            add(
                "failure_recovery_closed_loop",
                all(
                    item.get("status") == "completed"
                    for item in tasks
                ),
                {
                    "failure_injected": True,
                    "final_task_statuses": [
                        item.get("status") for item in tasks
                    ],
                },
            )
    if summary.get("risk_scenario_id"):
        levels = [item.get("level") for item in assessments]
        add(
            "dynamic_risk_assessment",
            bool(assessments),
            {"levels": levels},
        )
        add(
            "impact_and_prevention_explained",
            len(guidance) == len(assessments) and bool(guidance),
            {
                "assessment_count": len(assessments),
                "guidance_count": len(guidance),
            },
        )
        add(
            "risk_actions_generated",
            bool(summary.get("risk_task_ids"))
            or bool(summary.get("hazard_information_retained")),
            {
                "risk_task_ids": summary.get("risk_task_ids", []),
                "hazard_information_retained": summary.get(
                    "hazard_information_retained", False
                ),
            },
        )
        if mode != "mock":
            work_orders = summary.get("work_orders", [])
            if work_orders or summary.get(
                "road_restriction_required", True
            ):
                add(
                    "risk_work_orders_closed",
                    bool(work_orders)
                    and all(
                        item.get("status") == "closed"
                        for item in work_orders
                    ),
                    {
                        "closed": sum(
                            item.get("status") == "closed"
                            for item in work_orders
                        ),
                        "total": len(work_orders),
                    },
                )
            else:
                add(
                    "dynamic_takeover_task_completed",
                    bool(summary.get("takeover_completed")),
                    {
                        "work_order_required": False,
                        "takeover_completed": summary.get(
                            "takeover_completed", False
                        ),
                    },
                )
            add(
                "monitoring_dispatch_feedback_closed_loop",
                bool(summary.get("monitoring_dispatch_closed_loop"))
                and (
                    int(summary.get("closed_loop_feedback_count", 0)) > 0
                    or bool(summary.get("takeover_completed"))
                ),
                {
                    "feedback_count": summary.get(
                        "closed_loop_feedback_count", 0
                    ),
                    "decisions": summary.get(
                        "closed_loop_decision_counts", {}
                    ),
                    "closed_loop": summary.get(
                        "monitoring_dispatch_closed_loop", False
                    ),
                    "takeover_completed": summary.get(
                        "takeover_completed", False
                    ),
                },
            )
        if "red" in levels:
            if summary.get("road_restriction_required", True):
                add(
                    "red_risk_restriction_activated",
                    bool(summary.get("road_restrictions")),
                    {
                        "restriction_count": len(
                            summary.get("road_restrictions", [])
                        ),
                        "route_avoidance_enforced": summary.get(
                            "route_avoidance_enforced", False
                        ),
                    },
                )
            else:
                add(
                    "red_risk_information_retained",
                    bool(summary.get("hazard_information_retained")),
                    {
                        "road_closure_required": False,
                        "takeover_completed": summary.get(
                            "takeover_completed", False
                        ),
                    },
                )
    add(
        "offline_learning_dataset_exported",
        experience_count == len(tasks) and experience_count > 0,
        {
            "experience_count": experience_count,
            "training_status": "not_trained",
        },
    )
    memory = summary.get("imitation_memory", {})
    add(
        "bounded_imitation_memory_available",
        int(memory.get("preference_count", 0)) > 0,
        {
            "successful_experience_count": memory.get(
                "successful_experience_count", 0
            ),
            "preference_count": memory.get(
                "preference_count", 0
            ),
            "maximum_score_bonus_m": memory.get(
                "maximum_score_bonus_m", MAX_IMITATION_BONUS_M
            ),
            "safety_constraints_overridable": False,
        },
        required=False,
    )
    required = [item for item in checks if item["required"]]
    passed = sum(item["passed"] for item in required)
    return {
        "report_version": "functional-acceptance-v1",
        "run_id": summary.get("run_id"),
        "scenario_id": summary.get("scenario_id"),
        "overall_status": (
            "PASS" if required and passed == len(required) else "FAIL"
        ),
        "functional_score": round(
            passed / float(len(required)), 4
        )
        if required
        else 0.0,
        "passed_checks": passed,
        "required_checks": len(required),
        "checks": checks,
        "scope": (
            "CARLA 0325_5露天矿仿真地图下的功能闭环验收，"
            "不是矿山工业安全认证"
            if str(summary.get("scenario_id", "")).startswith("openpit-mine")
            else (
                "Town03普通车辆代理下的软件功能闭环验收，"
                "不是矿山工业安全认证"
            )
        ),
        "limitations": [
            "风险数据和阈值为合成演示数据，需用真实矿区数据重新标定。",
            "当前策略为透明规则基线，已导出经验但尚未训练模型。",
            "道路限制为策略层约束，尚未实现CARLA路网边级绕行。",
            "普通车辆仅代理验证协同机制，不能代表矿用装备性能。",
            "真实部署需要人工确认、故障安全设计和现场合规验证。",
        ],
    }


def finalize_learning_and_acceptance(
    recorder: Any,
    summary: Dict[str, Any],
    decision_records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write learning and acceptance artifacts and enrich the summary."""

    experiences = build_experience_dataset(summary)
    recorder.write_jsonl(
        "agent_decisions.jsonl", decision_records
    )
    recorder.write_jsonl(
        "experience_dataset.jsonl", experiences
    )
    summary["decision_intelligence"] = {
        "policy_version": POLICY_VERSION,
        "decision_record_count": len(decision_records),
        "decision_artifact": "agent_decisions.jsonl",
        "learning_mode": "offline_dataset_only",
        "experience_count": len(experiences),
        "experience_artifact": "experience_dataset.jsonl",
        "model_trained": False,
        "bounded_imitation_memory_applied": bool(
            summary.get("imitation_memory", {}).get(
                "preference_count", 0
            )
        ),
    }
    report = build_acceptance_report(
        summary, len(decision_records), len(experiences)
    )
    recorder.write_json("acceptance_report.json", report)
    summary["acceptance_report"] = {
        "artifact": "acceptance_report.json",
        "overall_status": report["overall_status"],
        "functional_score": report["functional_score"],
        "passed_checks": report["passed_checks"],
        "required_checks": report["required_checks"],
    }
    return report


def _environment_notes(environment: Dict[str, Any]) -> List[str]:
    notes = []
    dust = float(environment.get("dust_intensity_0_1", 0.0))
    visibility = float(environment.get("visibility_m", 999999.0))
    wind = float(environment.get("wind_speed_mps", 0.0))
    latency = float(
        environment.get("communication_latency_ms", 0.0)
    )
    if dust >= 0.3:
        notes.append("粉尘偏高：相机结果需与激光雷达或固定监测交叉验证。")
    if visibility < 800.0:
        notes.append("能见度偏低：降低行驶速度并扩大车辆安全间距。")
    if wind >= 8.0:
        notes.append("风速偏高：提高边坡表面变化和扬尘影响的关注度。")
    if latency >= 100.0:
        notes.append("通信存在延迟：关键管控动作需要回执和超时重试。")
    return notes or ["当前环境变量未触发额外的演示注意项。"]
