"""Single structural-scenario dispatch point for the supported baseline set."""
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from ..adapters import ExecutionCommand, ExecutionFeedback
from ..closed_loop import ClosedLoopCoordinator
from ..decision import SafetyShield
from ..runtime_state import RuntimeState
from .mock_runner import run_s01_structural_mock
from .s02_runner import run_random_s02_structural_mock, run_s02_structural_mock
from .s03_runner import run_random_s03_structural_mock
from .s04_runner import run_random_s04_structural_mock
from .s05_runner import run_random_s05_structural_mock
from .s06_runner import run_random_s06_structural_mock
from .s07_runner import run_random_s07_structural_mock, run_s07_structural_mock
from .s09_runner import run_random_s09_structural_mock
from .random_s01 import run_random_s01_structural_mock
from .models import ScenarioLifecycle, normalize_scenario_run_result
from .catalog import load_scenario_catalog


SCENARIO_CATALOG = load_scenario_catalog()
SUPPORTED_STRUCTURAL_SCENARIOS = tuple(
    key for key, detail in SCENARIO_CATALOG.items()
    if "structural" in detail["modes"]
)


def _apply_structural_safety_gate(
    result: Dict[str, Any], scenario_key: str
) -> Dict[str, Any]:
    """Validate explicit scenario decision facts before accepting the result."""
    policy_safety = result.get("policy_comparison", {}).get("safety_shield", {})
    if scenario_key in {"s01", "s02"} and isinstance(policy_safety, dict) \
            and policy_safety.get("reviews"):
        reviews = list(policy_safety["reviews"])
        rejected = [item for item in reviews if item.get("status") == "REJECTED"]
        mode = str(policy_safety.get("mode") or "shadow_evaluation_only")
        result["safety_shield"] = {
            "mode": mode,
            "status": (
                "FAIL" if rejected else
                "PASS" if mode == "execution_gate" else "SHADOW_PASS"
            ),
            "review_count": len(reviews),
            "rejected_count": len(rejected),
            "reviews": reviews,
            "physical_execution_gate": False,
        }
        if rejected and mode == "execution_gate":
            result["status"] = "FAIL"
            result["error"] = "optimized decision rejected by Safety Shield"
        return result
    collections = {
        "s03": ("equipment_decisions",),
        "s04": ("blast_decisions",),
        "s05": ("weather_decisions",),
        "s06": ("traffic_decisions",),
        "s07": ("route_changes",),
        "s09": ("route_changes", "compound_failure_decisions"),
    }
    selected_collections = collections.get(scenario_key)
    if selected_collections is None:
        return result
    shield = SafetyShield()
    reviews = []
    for collection in selected_collections:
        for decision in result.get(collection, []):
            if not isinstance(decision, dict):
                continue
            review = shield.review_explicit(
                vehicle_id=str(
                    decision.get("selected_vehicle_id")
                    or decision.get("vehicle_id") or "NOT_AVAILABLE"
                ),
                task_id=str(decision.get("task_id") or "NOT_AVAILABLE"),
                constraint_results=decision.get("constraint_results"),
            ).to_dict()
            decision["safety_review"] = review
            reviews.append(review)
    rejected = [item for item in reviews if item["status"] == "REJECTED"]
    result["safety_shield"] = {
        "mode": "structural_result_gate_before_acceptance",
        "status": (
            "PASS" if reviews and not rejected
            else "NOT_APPLICABLE" if not reviews else "FAIL"
        ),
        "review_count": len(reviews),
        "rejected_count": len(rejected),
        "reviews": reviews,
        "physical_execution_gate": False,
    }
    if rejected:
        result["status"] = "FAIL"
        result["error"] = "structural decision rejected by Safety Shield"
    return result


def _attach_structural_execution_contract(
    result: Dict[str, Any], scenario_key: str
) -> Dict[str, Any]:
    """Bridge legacy structural outcomes to the common execution contract.

    This is deliberately labelled as post-execution reconstruction.  It does
    not claim that a physical Safety Shield or CARLA controller executed the
    event decision.
    """
    if result.get("execution_commands") or result.get("execution_feedback"):
        return result
    tasks = [dict(item) for item in result.get("tasks", [])
             if isinstance(item, dict) and item.get("task_id")]
    assignments = {
        str(item["task_id"]): str(item["assigned_vehicle_id"])
        for item in tasks if item.get("assigned_vehicle_id") is not None
    }
    shield_status = result.get("safety_shield", {}).get("status")
    if shield_status == "PASS":
        gate_status = "VALIDATED_POST_EXECUTION_STRUCTURAL_ONLY"
    elif shield_status == "FAIL":
        gate_status = "REJECTED"
    else:
        gate_status = "NOT_AVAILABLE_LEGACY_BASELINE"
    command = ExecutionCommand(
        command_id="{}:structural-execution".format(
            result.get("scenario_id") or scenario_key
        ),
        action_type="dispatch_tasks",
        task_ids=tuple(item["task_id"] for item in tasks),
        assignments=assignments,
        issued_by=str(
            result.get("policy_version") or "structural_scenario_runner"
        ),
        safety_gate_status=gate_status,
        metadata={
            "scenario_key": scenario_key,
            "contract_source": "structural_result_compatibility_bridge",
            "timing": "post_execution_reconstruction",
            "physical_execution_gate": False,
        },
    )
    completed = int(result.get("completed_task_count") or 0)
    task_count = int(result.get("task_count") or len(tasks))
    feedback = ExecutionFeedback(
        command_id=command.command_id,
        phase="terminal_reconstruction",
        status=(
            "SUCCEEDED" if result.get("status") == "PASS"
            and task_count > 0 and completed == task_count else "FAILED"
        ),
        adapter_type="StructuralResultCompatibilityBridge",
        physical_execution=False,
        measurement_status="STRUCTURAL_ONLY_NO_PHYSICS",
        safety_gate_status=gate_status,
        task_states=tasks,
        vehicle_states=[
            dict(item) for item in result.get("final_vehicle_states", [])
            if isinstance(item, dict)
        ],
        events=[],
        error=result.get("error"),
    )
    result["execution_commands"] = [command.to_dict()]
    result["execution_feedback"] = [feedback.to_dict()]
    result["execution_contract_mode"] = (
        "post_execution_structural_compatibility_bridge"
    )
    return result


def _coordinate_structural_result(
    result: Dict[str, Any], scenario_key: str, config: Any
) -> Dict[str, Any]:
    """Run legacy scenario facts through the common state/feedback cycle.

    Scenario algorithms still calculate their established result.  Unlike the
    compatibility bridge, this path starts from a task snapshot captured
    before completion and lets the shared RuntimeState consume the factual
    terminal feedback.  It is structural execution, never CARLA physics.
    """
    if result.get("closed_loop_cycle"):
        return result
    initial_tasks = result.get("initial_task_states")
    final_tasks = result.get("tasks")
    if not isinstance(initial_tasks, list) or not isinstance(final_tasks, list):
        return result
    decisions_by_scenario = {
        "s03": ("equipment_decisions",),
        "s04": ("blast_decisions",),
        "s05": ("weather_decisions",),
        "s06": ("traffic_decisions",),
        "s07": ("route_changes",),
        "s09": ("route_changes", "compound_failure_decisions"),
    }
    collections = decisions_by_scenario.get(scenario_key)
    if collections is None:
        return result
    decisions = []
    for name in collections:
        decisions.extend(
            dict(item) for item in result.get(name, [])
            if isinstance(item, dict)
        )
    map_resource = getattr(config, "map_resource", None)
    map_context = {
        "map_id": getattr(map_resource, "map_id", None),
        "resource_version": getattr(map_resource, "resource_version", None),
        "source": result.get("scenario_source"),
    }
    assignment_map = {
        str(item.get("task_id")): str(item.get("assigned_vehicle_id"))
        for item in final_tasks
        if isinstance(item, dict) and item.get("task_id")
        and item.get("assigned_vehicle_id") is not None
    }
    command = ExecutionCommand(
        command_id="{}:coordinated-structural-execution".format(
            result.get("scenario_id") or scenario_key
        ),
        action_type="dispatch_tasks",
        task_ids=tuple(
            str(item.get("task_id")) for item in final_tasks
            if isinstance(item, dict) and item.get("task_id")
        ),
        assignments=assignment_map,
        issued_by=str(result.get("policy_version") or "structural_policy"),
        safety_gate_status="APPROVED",
        metadata={
            "scenario_key": scenario_key,
            "simulation_mode": "structural",
            "physical_execution": False,
        },
    ).to_dict()
    safety = result.get("safety_shield", {})
    if not isinstance(safety, dict):
        safety = {}
    safety_status = (
        "APPROVED" if safety.get("status") in {"PASS", "SHADOW_PASS"}
        else "REJECTED" if safety.get("status") == "FAIL" else "UNKNOWN"
    )
    event_payload = {
        key: result.get(key) for key in (
            "equipment_event", "blast_event", "weather_event",
            "congestion_event", "closed_edge_id", "compound_events",
            "failed_vehicle_id",
        ) if result.get(key) is not None
    }
    initial_state = {
        "schema_version": "openpit.world-state.v1",
        "run_id": result.get("scenario_id"),
        "vehicles": [
            dict(item) for item in result.get("initial_vehicle_states", [])
            if isinstance(item, dict)
        ],
        "tasks": [dict(item) for item in initial_tasks if isinstance(item, dict)],
        "roads": {
            "closed_edge_id": result.get("closed_edge_id"),
            "restricted_edge_id": result.get("restricted_edge_id"),
            "degraded_edge_id": result.get("degraded_edge_id"),
            "bottleneck_edge_id": result.get("bottleneck_edge_id"),
        },
        "environment": {"map_context": map_context},
        "monitoring": {},
        "risk": event_payload,
        "traffic": result.get("congestion_event", {}),
        "equipment": result.get("equipment_event", {}),
    }

    def fixed(payload):
        return lambda context: dict(payload)

    coordinator = ClosedLoopCoordinator(
        RuntimeState(),
        risk_stage=fixed({
            "status": "EVENT_EVALUATED",
            "scenario_key": scenario_key,
            "event": event_payload,
        }),
        decision_stage=fixed({
            "status": "DECIDED",
            "policy_version": result.get("policy_version"),
            "decisions": decisions,
        }),
        scheduling_stage=fixed({
            "status": "SCHEDULED",
            "assignments": assignment_map,
            "command": command,
        }),
        planning_stage=fixed({
            "status": "PLANNED",
            "planner_version": result.get("route_planner_version"),
            "route_plans": result.get("route_plans", []),
            "physical_route_execution": False,
        }),
        safety_stage=fixed({
            "status": safety_status,
            "reviews": safety.get("reviews", []),
            "shield_mode": safety.get("mode"),
        }),
        execution_stage=lambda context: ExecutionFeedback(
            command_id=command["command_id"],
            phase="terminal",
            status=("SUCCEEDED" if result.get("status") == "PASS" else "FAILED"),
            adapter_type="StructuralScenarioRunner",
            physical_execution=False,
            measurement_status="STRUCTURAL_ONLY_NO_PHYSICS",
            safety_gate_status=safety_status,
            task_states=[
                dict(item) for item in final_tasks if isinstance(item, dict)
            ],
            vehicle_states=[
                dict(item) for item in result.get("final_vehicle_states", [])
                if isinstance(item, dict)
            ],
            events=[],
            error=result.get("error"),
        ).to_dict(),
    )
    cycle = coordinator.run_cycle(initial_state)
    result["closed_loop_cycle"] = cycle
    result["closed_loop_coordination"] = "executed"
    result["execution_commands"] = [command]
    result["execution_feedback"] = list(cycle.get("execution_feedback", []))
    result["execution_contract_mode"] = "coordinated_structural_execution"
    return result


def validate_structural_closed_loop(result: Dict[str, Any]) -> Dict[str, Any]:
    """Validate scenario semantics from the resolved in-memory result."""
    scenario = str(result.get("scenario_key") or "")
    checks = []

    def check(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    tasks = result.get("tasks", [])
    task_count = int(result.get("task_count") or len(tasks))
    completed = int(result.get("completed_task_count") or 0)
    check("run_status", result.get("status") == "PASS", result.get("status"))
    check("task_count_positive", task_count > 0, task_count)
    check("all_tasks_completed", task_count > 0 and completed == task_count,
          "{}/{}".format(completed, task_count))
    check("task_terminal_states", len(tasks) == task_count and all(
        isinstance(task, dict) and task.get("status") == "completed" for task in tasks
    ), "completed task payloads={}".format(sum(
        isinstance(task, dict) and task.get("status") == "completed" for task in tasks
    )))

    if scenario == "s01":
        assignments = result.get("assignments", [])
        check("s01_all_tasks_assigned", len(assignments) == task_count,
              len(assignments))
        assigned_vehicle_ids = [
            item.get("vehicle_id") for item in assignments if isinstance(item, dict)
        ]
        check("s01_assignments_reference_vehicle", all(assigned_vehicle_ids),
              assigned_vehicle_ids)
        if result.get("random_mode"):
            check("s01_unique_vehicle_assignment", len(set(
                assigned_vehicle_ids
            )) == task_count, "unique vehicles")
    elif scenario == "s02":
        released = set(result.get("released_task_ids", []))
        reassigned = result.get("assignments", [])
        failed = result.get("failed_vehicle_id")
        check("s02_failure_recorded", bool(failed), failed)
        check("s02_task_released", bool(released), sorted(released))
        check("s02_all_released_tasks_reassigned", released == {
            item.get("task_id") for item in reassigned if isinstance(item, dict)
        }, "released={} reassigned={}".format(len(released), len(reassigned)))
        check("s02_failed_vehicle_excluded", all(
            item.get("vehicle_id") != failed for item in reassigned
            if isinstance(item, dict)
        ), failed)
    elif scenario == "s03":
        decisions = result.get("equipment_decisions", [])
        affected = int(result.get("affected_task_count") or 0)
        check("s03_synthetic_origin_declared", result.get(
            "equipment_event", {}
        ).get("data_origin") == "PARAMETERIZED_SYNTHETIC_SCENARIO",
              result.get("equipment_event", {}).get("data_origin"))
        check("s03_selective_task_impact", affected > 0 and int(
            result.get("unaffected_task_count") or 0
        ) > 0, "affected={}".format(affected))
        check("s03_all_affected_tasks_switched", len(decisions) == affected,
              "{}/{}".format(len(decisions), affected))
        check("s03_failed_work_point_excluded", all(
            item.get("failed_work_point_id")
            != item.get("alternative_work_point_id")
            and item.get("constraint_results", {}).get(
                "failed_equipment_excluded") is True
            for item in decisions if isinstance(item, dict)
        ), "alternative work point differs from failed point")
        check("s03_alternative_route_available", all(
            item.get("constraint_results", {}).get(
                "alternative_route_reachable") is True
            and item.get("measurement_status")
                == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            for item in decisions if isinstance(item, dict)
        ), "route fact and timing provenance declared")
        check("s03_equipment_recovered", result.get(
            "equipment_status_after_recovery") == "AVAILABLE",
              result.get("equipment_status_after_recovery"))
    elif scenario == "s04":
        decisions = result.get("blast_decisions", [])
        affected = int(result.get("affected_task_count") or 0)
        restricted_edge = result.get("restricted_edge_id")
        check("s04_synthetic_origin_declared", result.get(
            "blast_event", {}
        ).get("data_origin") == "PARAMETERIZED_SYNTHETIC_SCENARIO",
              result.get("blast_event", {}).get("data_origin"))
        check("s04_topology_anchor_declared", str(restricted_edge).startswith(
            "carla-topology-edge:"
        ) and result.get("blast_event", {}).get(
            "exclusion_scope") == "TOPOLOGY_EDGE_ANCHOR_ONLY", restricted_edge)
        check("s04_selective_impact", affected > 0 and int(
            result.get("unaffected_task_count") or 0
        ) > 0, "affected={}".format(affected))
        check("s04_all_affected_tasks_controlled", len(decisions) == affected,
              "{}/{}".format(len(decisions), affected))
        check("s04_safe_action_only", all(
            item.get("action_type") in {
                "blast_zone_safe_route", "hold_until_blast_clearance"
            } and (
                item.get("action_type") == "hold_until_blast_clearance"
                or restricted_edge not in item.get("replanned_edge_ids", [])
            ) for item in decisions if isinstance(item, dict)
        ), "detour avoids control edge or waits for clearance")
        check("s04_no_takeover_for_temporary_control", int(
            result.get("takeover_count") or 0
        ) == 0, result.get("takeover_count"))
        check("s04_control_released", result.get(
            "blast_zone_status_after_clearance") == "CLEARED"
            and result.get("road_status_after_clearance") == "OPEN",
              result.get("blast_zone_status_after_clearance"))
    elif scenario == "s07":
        closed_edge = result.get("closed_edge_id")
        changes = result.get("route_changes", [])
        affected = int(result.get("affected_task_count") or 0)
        check("s07_real_topology_edge", str(closed_edge).startswith(
            "carla-topology-edge:"
        ), closed_edge)
        check("s07_selective_impact", affected > 0 and int(
            result.get("unaffected_task_count") or 0
        ) > 0, "affected={}".format(affected))
        check("s07_all_affected_tasks_resolved", len(changes) == affected,
              "{}/{}".format(len(changes), affected))
        check("s07_closed_edge_avoided", all(
            closed_edge not in item.get("replanned_edge_ids", [])
            for item in changes if isinstance(item, dict)
        ), closed_edge)
        check("s07_resolution_counts", (
            int(result.get("same_vehicle_replan_count") or 0)
            + int(result.get("takeover_count") or 0) == affected
        ), "same={} takeover={}".format(
            result.get("same_vehicle_replan_count"), result.get("takeover_count")
        ))
        check("s07_road_reopened", result.get("road_status_after_reopen") == "OPEN",
              result.get("road_status_after_reopen"))
    elif scenario == "s05":
        decisions = result.get("weather_decisions", [])
        affected = int(result.get("affected_task_count") or 0)
        check("s05_synthetic_origin_declared", result.get(
            "weather_event", {}
        ).get("data_origin") == "PARAMETERIZED_SYNTHETIC_SCENARIO",
              result.get("weather_event", {}).get("data_origin"))
        check("s05_selective_impact", affected > 0 and int(
            result.get("unaffected_task_count") or 0
        ) > 0, "affected={}".format(affected))
        check("s05_all_affected_tasks_decided", len(decisions) == affected,
              "{}/{}".format(len(decisions), affected))
        check("s05_eta_provenance", all(
            item.get("measurement_status") == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            and item.get("estimated_delay_s") is not None
            for item in decisions if isinstance(item, dict)
        ), "weather decision ETA source declared")
        check("s05_road_restored", result.get(
            "road_status_after_recovery"
        ) == "OPEN", result.get("road_status_after_recovery"))
        check("s05_weather_recovered", result.get(
            "weather_status_after_recovery"
        ) == "clear", result.get("weather_status_after_recovery"))
    elif scenario == "s06":
        decisions = result.get("traffic_decisions", [])
        affected = int(result.get("affected_task_count") or 0)
        headway = float(result.get("congestion_event", {}).get(
            "minimum_safety_headway_seconds") or 0.0)
        schedule_safe = all(
            float(current.get("scheduled_entry_s") or 0.0)
            + 1e-6 >= float(previous.get("scheduled_exit_s") or 0.0) + headway
            for previous, current in zip(decisions, decisions[1:])
        )
        check("s06_synthetic_origin_declared", result.get(
            "congestion_event", {}
        ).get("data_origin") == "PARAMETERIZED_SYNTHETIC_SCENARIO",
              result.get("congestion_event", {}).get("data_origin"))
        check("s06_selective_shared_road_impact", affected >= 2 and int(
            result.get("unaffected_task_count") or 0
        ) > 0, "affected={}".format(affected))
        check("s06_all_affected_tasks_scheduled", len(decisions) == affected,
              "{}/{}".format(len(decisions), affected))
        check("s06_surrogate_provenance", all(
            item.get("measurement_status") == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            and float(item.get("estimated_wait_s") or 0.0) >= 0.0
            for item in decisions if isinstance(item, dict)
        ), "queue time source declared")
        check("s06_capacity_and_headway_respected", schedule_safe,
              "headway={}s".format(headway))
        check("s06_traffic_control_released", result.get(
            "traffic_control_status_after_recovery") == "RELEASED",
              result.get("traffic_control_status_after_recovery"))
    elif scenario == "s09":
        events = result.get("compound_events", [])
        fault_decisions = result.get("compound_failure_decisions", [])
        closed_edge = result.get("closed_edge_id")
        check("s09_two_ordered_events", len(events) == 2
              and [item.get("event_type") for item in events]
              == ["road_closure", "vehicle_failure"]
              and int(events[0].get("tick")) < int(events[1].get("tick")),
              [item.get("event_type") for item in events])
        check("s09_single_world_state", result.get("scenario_source")
              == "single_map_resource_workload_with_ordered_compound_events",
              result.get("scenario_source"))
        check("s09_compound_admission", result.get(
            "scenario_admission_status") == "COMPOUND_FEASIBLE"
              and int(result.get("generation_attempt") or 0) < 20,
              "attempt={} workload_seed={}".format(
                  result.get("generation_attempt"),
                  result.get("workload_seed")))
        check("s09_fault_task_released_and_reassigned", len(
            result.get("released_task_ids", [])) == 1
            and len(fault_decisions) == 1
            and int(result.get("reassignment_count") or 0) == 1,
              result.get("released_task_ids"))
        check("s09_failed_vehicle_excluded", all(
            item.get("selected_vehicle_id") != result.get("failed_vehicle_id")
            for item in fault_decisions if isinstance(item, dict)
        ), result.get("failed_vehicle_id"))
        check("s09_takeover_route_avoids_closed_edge", all(
            closed_edge not in item.get("route_edge_ids", [])
            and all(item.get("constraint_results", {}).values())
            for item in fault_decisions if isinstance(item, dict)
        ), closed_edge)
        check("s09_road_and_fault_terminal_state",
              result.get("road_status_after_reopen") == "OPEN"
              and result.get("failed_vehicle_status_after_run")
                  == "FAILED_ISOLATED",
              "road={} vehicle={}".format(
                  result.get("road_status_after_reopen"),
                  result.get("failed_vehicle_status_after_run")))
        failed_final_states = [
            item for item in result.get("final_vehicle_states", [])
            if isinstance(item, dict)
            and item.get("vehicle_id") == result.get("failed_vehicle_id")
        ]
        check("s09_failed_vehicle_state_consistent",
              len(failed_final_states) == 1
              and failed_final_states[0].get("health") == "fault"
              and failed_final_states[0].get("available") is False
              and failed_final_states[0].get("task_status")
                  == "failed_isolated",
              failed_final_states)

    failed_checks = [item["check"] for item in checks if not item["passed"]]
    return {
        "status": "CLOSED_LOOP_PASS" if not failed_checks else "CLOSED_LOOP_FAIL",
        "scenario_key": scenario,
        "check_count": len(checks),
        "passed_check_count": len(checks) - len(failed_checks),
        "failed_checks": failed_checks,
        "checks": checks,
    }


def summarize_structural_batch(results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate factual structural-run outcomes without inventing metrics."""
    items = list(results)
    by_scenario: Dict[str, Dict[str, Any]] = {}
    total_tasks = completed_tasks = 0
    for result in items:
        key = str(result.get("scenario_key") or "unknown")
        summary = by_scenario.setdefault(key, {
            "run_count": 0, "pass_count": 0, "fail_count": 0,
            "task_count": 0, "completed_task_count": 0,
            "released_task_count": 0, "reassigned_task_count": 0,
            "affected_task_count": 0, "replanned_task_count": 0,
            "same_vehicle_replan_count": 0, "takeover_count": 0,
            "closed_loop_pass_count": 0,
            "errors": [],
        })
        summary["run_count"] += 1
        passed = result.get("status") == "PASS"
        summary["pass_count" if passed else "fail_count"] += 1
        task_count = int(result.get("task_count") or 0)
        completed = int(result.get("completed_task_count") or 0)
        summary["task_count"] += task_count
        summary["completed_task_count"] += completed
        summary["released_task_count"] += len(result.get("released_task_ids", []))
        summary["reassigned_task_count"] += int(result.get("reassignment_count") or 0)
        summary["affected_task_count"] += int(result.get("affected_task_count") or 0)
        summary["replanned_task_count"] += int(result.get("replanned_task_count") or 0)
        summary["same_vehicle_replan_count"] += int(result.get("same_vehicle_replan_count") or 0)
        summary["takeover_count"] += int(result.get("takeover_count") or 0)
        if result.get("closed_loop_status") == "CLOSED_LOOP_PASS":
            summary["closed_loop_pass_count"] += 1
        if result.get("error"):
            summary["errors"].append(str(result["error"]))
        total_tasks += task_count
        completed_tasks += completed
    for key, summary in by_scenario.items():
        summary["run_pass_rate"] = (
            summary["pass_count"] / summary["run_count"]
            if summary["run_count"] else None
        )
        summary["task_completion_rate"] = (
            summary["completed_task_count"] / summary["task_count"]
            if summary["task_count"] else None
        )
        summary["failure_reassignment_rate"] = (
            summary["reassigned_task_count"] / summary["released_task_count"]
            if summary["released_task_count"] else None
        )
        summary["route_replan_success_rate"] = (
            summary["replanned_task_count"] / summary["affected_task_count"]
            if key in {"s04", "s07", "s09"}
            and summary["affected_task_count"] else None
        )
        summary["closed_loop_pass_rate"] = (
            summary["closed_loop_pass_count"] / summary["run_count"]
            if summary["run_count"] else None
        )
    return {
        "status": "PASS" if items and all(
            item.get("status") == "PASS" for item in items
        ) else "FAIL",
        "run_count": len(items),
        "pass_count": sum(item.get("status") == "PASS" for item in items),
        "fail_count": sum(item.get("status") != "PASS" for item in items),
        "task_count": total_tasks,
        "completed_task_count": completed_tasks,
        "closed_loop_pass_count": sum(
            item.get("closed_loop_status") == "CLOSED_LOOP_PASS" for item in items
        ),
        "closed_loop_pass_rate": (
            sum(item.get("closed_loop_status") == "CLOSED_LOOP_PASS" for item in items)
            / len(items) if items else None
        ),
        "task_completion_rate": (
            completed_tasks / total_tasks if total_tasks else None
        ),
        "by_scenario": by_scenario,
    }


def run_structural_scenario(scenario: str, config: Any,
                            seed: Optional[int] = None, random_map: bool = False,
                            vehicle_count: int = 6,
                            execution_policy: str = "heuristic",
                            eligible_pairs: Optional[Set[Tuple[str, str]]] = None,
                            minimum_length_m: float = 500.0,
                            maximum_length_m: float = 3000.0) -> Dict[str, Any]:
    """Run an existing baseline runner without changing its semantics.

    This is intentionally a small dispatch layer, not a new simulation
    engine.  It lets future scenario configuration and batch execution use
    one stable public entry point while preserving each baseline runner.
    """
    name = str(scenario).lower()
    lifecycle = ScenarioLifecycle()
    lifecycle.mark("prepare", "completed", {
        "scenario_key": name,
        "seed": seed,
        "random_map": bool(random_map),
        "vehicle_count": vehicle_count,
    })
    runners = {
        "s01": run_s01_structural_mock,
        "s02": run_s02_structural_mock,
        "s03": run_random_s03_structural_mock,
        "s04": run_random_s04_structural_mock,
        "s05": run_random_s05_structural_mock,
        "s06": run_random_s06_structural_mock,
        "s07": run_s07_structural_mock,
        "s09": run_random_s09_structural_mock,
    }
    if name not in runners:
        raise ValueError("unsupported structural scenario: {}; supported={}".format(
            scenario, ", ".join(SUPPORTED_STRUCTURAL_SCENARIOS)))
    if execution_policy not in {"heuristic", "multi-objective"}:
        raise ValueError("unsupported execution policy: {}".format(execution_policy))
    if execution_policy == "multi-objective" and (
            name not in {"s01", "s02"} or not random_map):
        raise ValueError(
            "multi-objective execution currently supports random-map S01/S02 only"
        )
    if name in {"s03", "s04", "s05", "s06", "s09"} and not random_map:
        raise ValueError("{} requires --random-map and map_resources.db".format(
            name.upper()))
    lifecycle.mark("start", "completed", {
        "execution_mode": "structural",
        "execution_policy": execution_policy,
    })
    if random_map:
        random_runners = {
            "s01": run_random_s01_structural_mock,
            "s02": run_random_s02_structural_mock,
            "s03": run_random_s03_structural_mock,
            "s04": run_random_s04_structural_mock,
            "s05": run_random_s05_structural_mock,
            "s06": run_random_s06_structural_mock,
            "s07": run_random_s07_structural_mock,
            "s09": run_random_s09_structural_mock,
        }
        arguments = {
            "seed": seed, "vehicle_count": vehicle_count,
            "minimum_length_m": minimum_length_m,
            "maximum_length_m": maximum_length_m,
        }
        if name in {"s01", "s02"}:
            arguments["execution_policy"] = execution_policy
            arguments["eligible_pairs"] = eligible_pairs
        elif eligible_pairs is not None:
            arguments["eligible_pairs_override"] = eligible_pairs
        result = random_runners[name](config, **arguments)
    else:
        if name == "s01":
            result = run_s01_structural_mock(
                config, seed=seed, include_candidate_rankings=True
            )
        else:
            result = runners[name](config, seed=seed)
    result = _apply_structural_safety_gate(result, name)
    result = _coordinate_structural_result(result, name, config)
    result = _attach_structural_execution_contract(result, name)
    if name == "s01" and not result.get("policy_comparison"):
        result["policy_comparison"] = {
            "mode": "heuristic_baseline_execution",
            "requested_policy": execution_policy,
            "executed_policy": "heuristic-distance-load-v0",
            "shadow_policy": None,
            "optimizer_version": None,
            "comparisons": [],
        }
    compound_events = result.get("compound_events")
    if isinstance(compound_events, list):
        observed_event_count = len(compound_events)
    else:
        observed_event_count = sum(
            isinstance(result.get(key), dict) and bool(result.get(key))
            for key in (
                "equipment_event", "blast_event", "weather_event",
                "congestion_event",
            )
        )
        if not observed_event_count and (
                result.get("failed_vehicle_id") or result.get("closed_edge_id")):
            observed_event_count = 1
    lifecycle.mark(
        "event", "completed" if observed_event_count else "not_applicable",
        {"observed_event_count": observed_event_count},
    )
    decision_lists = [
        result.get(key) for key in (
            "assignments", "equipment_decisions", "blast_decisions",
            "weather_decisions", "traffic_decisions", "route_changes",
            "compound_failure_decisions",
        )
    ]
    observed_decision_count = max(
        [len(items) for items in decision_lists if isinstance(items, list)] or [0]
    )
    lifecycle.mark(
        "decision", "completed" if observed_decision_count else "not_applicable",
        {
            "observed_decision_count": observed_decision_count,
            "safety_shield_status": result.get("safety_shield", {}).get("status"),
            "safety_review_count": result.get("safety_shield", {}).get(
                "review_count"
            ),
        },
    )
    lifecycle.mark("execute", (
        "completed" if result.get("status") == "PASS" else "failed"
    ), {
        "mode": result.get("mode"),
        "task_count": result.get("task_count"),
        "completed_task_count": result.get("completed_task_count"),
    })
    result["runner_entry"] = "unified_structural_runner_v1"
    result["scenario_key"] = name
    result["requested_execution_policy"] = execution_policy
    map_resource = getattr(config, "map_resource", None)
    result["map_context"] = {
        "map_id": getattr(map_resource, "map_id", None),
        "resource_version": getattr(map_resource, "resource_version", None),
    }
    result["closed_loop_validation"] = validate_structural_closed_loop(result)
    result["closed_loop_status"] = result["closed_loop_validation"]["status"]
    lifecycle.mark("feedback", "completed", {
        "closed_loop_status": result["closed_loop_status"],
        "check_count": result["closed_loop_validation"].get("check_count"),
    })
    lifecycle.mark("finish", result.get("status", "UNKNOWN"), {
        "all_tasks_completed": (
            int(result.get("task_count") or 0) > 0
            and int(result.get("completed_task_count") or 0)
            == int(result.get("task_count") or 0)
        ),
    })
    result["lifecycle"] = lifecycle.to_dict()
    return normalize_scenario_run_result(
        result, scenario_key=name, map_context=result["map_context"]
    )
