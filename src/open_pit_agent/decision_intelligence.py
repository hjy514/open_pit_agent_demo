"""Explainable decisions, reusable experience data and run acceptance."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Assignment, Task
from .risk import RiskAssessment, RiskScenario, actions_for_assessment
from .scenario_runtime import ResolvedScenario


POLICY_VERSION = "capability-distance-load-transparent-v1"
GUIDANCE_VERSION = "slope-risk-guidance-demo-v1"
EXPERIENCE_SCHEMA_VERSION = "1.0"
MAX_IMITATION_BONUS_M = 2.0


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
            bool(summary.get("risk_task_ids")),
            {"risk_task_ids": summary.get("risk_task_ids", [])},
        )
        if mode != "mock":
            work_orders = summary.get("work_orders", [])
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
            add(
                "monitoring_dispatch_feedback_closed_loop",
                bool(summary.get("monitoring_dispatch_closed_loop"))
                and int(
                    summary.get("closed_loop_feedback_count", 0)
                ) > 0,
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
                },
            )
        if "red" in levels:
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
