"""Planned blasting and temporary road-control structural scenario."""
from copy import deepcopy
from typing import Any, Dict, Optional, Set, Tuple

from .generator import sample_event_timing
from .s07_runner import run_random_s07_structural_mock


POLICY_VERSION = "planned-blast-road-control-v1"


def _blast_parameters(config: Any, seed: int) -> Dict[str, Any]:
    timing = sample_event_timing(config, "s04", seed)
    notice_tick = timing["notice_tick"]
    start_tick = timing["blast_start_tick"]
    clearance_tick = timing["clearance_tick"]
    if not notice_tick < start_tick < clearance_tick:
        raise ValueError("S04 requires notice_tick < blast_start_tick < clearance_tick")
    return {
        "notice_tick": notice_tick,
        "blast_start_tick": start_tick,
        "clearance_tick": clearance_tick,
    }


def run_random_s04_structural_mock(config: Any, seed: Optional[int] = None,
                                   vehicle_count: int = 6,
                                   minimum_length_m: float = 500.0,
                                   maximum_length_m: float = 3000.0,
                                   eligible_pairs_override: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Reuse the road-constraint engine for a planned blast time window.

    A planned blast is a short, known control window, so affected vehicles
    hold until clearance and then continue their admitted physical mission.
    S07 still owns long-lived road-closure detours.  Keeping the two policies
    separate avoids replacing a short wait with a kilometre-scale topology
    detour whose distance is not comparable with the P6 physical-route fact.
    """
    try:
        result = run_random_s07_structural_mock(
            config, seed=seed, vehicle_count=vehicle_count,
            minimum_length_m=minimum_length_m,
            maximum_length_m=maximum_length_m, scenario_key="s04",
            eligible_pairs_override=eligible_pairs_override,
            allow_temporary_control_wait=True,
        )
    except ValueError as exc:
        raise ValueError(
            "seeded S04 workload has no selective temporary road-control "
            "action: {}".format(exc)
        ) from exc
    result = deepcopy(result)
    parameters = _blast_parameters(config, int(result["seed"]))
    restricted_edge_id = result.pop("closed_edge_id")
    result.pop("closed_road_id", None)
    task_by_id = {
        str(item.get("task_id")): item for item in result.get("tasks", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    original_plan_by_task = {
        str(item.get("task_id")): item for item in result.get("route_plans", [])
        if isinstance(item, dict) and item.get("task_id")
        and item.get("route_plan_id", "").endswith(":original")
    }
    planned_plan_by_task = {
        str(item.get("task_id")): item for item in result.get("route_plans", [])
        if isinstance(item, dict) and item.get("task_id")
        and item.get("route_plan_id", "").endswith(":replanned")
    }
    for change in result.get("route_changes", []):
        task_id = str(change["task_id"])
        original_vehicle_id = (
            change.get("original_vehicle_id") or change.get("vehicle_id")
        )
        original_edges = list(change.get("original_edge_ids", []))
        original = original_plan_by_task.get(task_id)
        change.update({
            "action_type": "hold_until_blast_clearance",
            "vehicle_id": original_vehicle_id,
            "replanned_edge_ids": original_edges,
            "replanned_distance_m": change.get("original_distance_m"),
            "takeover_required": False,
            "wait_until_tick": parameters["clearance_tick"],
            "reason": "planned_short_blast_wait_for_control_release",
            "route_contract": dict(
                original.get("route_contract", {}) if original else {}
            ),
        })
        task = task_by_id.get(task_id)
        if task is not None:
            task["assigned_vehicle_id"] = original_vehicle_id
            task["original_vehicle_id"] = None
            task["handover_reason"] = None
            task["handover_tick"] = None
            task["transfer_count"] = 0
            task["status_reason"] = "structural_mock_completion_after_blast_wait"
        planned = planned_plan_by_task.get(task_id)
        if planned is not None and original is not None:
            planned.update({
                "vehicle_id": original_vehicle_id,
                "start_node": original.get("start_node"),
                "goal_node": original.get("goal_node"),
                "distance_m": original.get("distance_m"),
                "status": "scheduled_after_blast_clearance",
                "replan_reason": "temporary_blast_control_wait",
                "edge_ids": list(original_edges),
                "closed_edge_ids": [],
                "route_contract": dict(original.get("route_contract", {})),
            })

    for task in result.get("tasks", []):
        if isinstance(task, dict):
            task["completed_tick"] = parameters["clearance_tick"]
    for plan in result.get("route_plans", []):
        if not isinstance(plan, dict):
            continue
        if plan.get("task_id") in result.get("route_impact_task_ids", []):
            if plan.get("route_plan_id", "").endswith(":original"):
                plan["replan_reason"] = "temporary_blast_control"
            elif plan.get("route_plan_id", "").endswith(":replanned"):
                plan["replan_reason"] = (
                    "temporary_blast_control_wait"
                    if plan.get("status") == "scheduled_after_blast_clearance"
                    else "temporary_blast_control_detour"
                )

    decisions = []
    for change in result.get("route_changes", []):
        decision = {
            **change,
            "restricted_edge_id": restricted_edge_id,
            "measurement_status": "STRUCTURAL_ROUTE_LOGIC_NOT_CARLA_MEASURED",
            "policy_version": POLICY_VERSION,
        }
        waits_for_clearance = (
            decision.get("action_type") == "hold_until_blast_clearance"
        )
        decision["constraint_results"] = {
            "temporary_control_action_valid": decision.get("action_type") in {
                "blast_zone_safe_route", "hold_until_blast_clearance"
            },
            "restricted_edge_not_entered_during_control": (
                waits_for_clearance
                or restricted_edge_id not in decision.get("replanned_edge_ids", [])
            ),
            "clearance_wait_defined_when_required": (
                not waits_for_clearance or decision.get("wait_until_tick") is not None
            ),
            "task_takeover_not_used_for_temporary_control": not bool(
                decision.get("takeover_required")
            ),
        }
        decisions.append(decision)
    detour_count = sum(
        item["action_type"] == "blast_zone_safe_route" for item in decisions
    )
    wait_count = sum(
        item["action_type"] == "hold_until_blast_clearance" for item in decisions
    )
    result.update({
        "simulation_claim": (
            "planned_blast_road_control_and_route_logic_only_no_carla_blast_physics"
        ),
        "scenario_source": "map_resources_topology_and_parameterized_blast_window",
        "random_mode": "seeded_structural_blast_control_mock_only",
        "blast_event": {
            "event_type": "planned_blasting_temporary_control",
            "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
            "restricted_edge_id": restricted_edge_id,
            "exclusion_scope": "TOPOLOGY_EDGE_ANCHOR_ONLY",
            **parameters,
        },
        "restricted_edge_id": restricted_edge_id,
        "blast_zone_status_during_event": "CONTROLLED",
        "blast_zone_status_after_clearance": "CLEARED",
        "road_status_after_clearance": "OPEN",
        "blast_decisions": decisions,
        "safe_detour_count": detour_count,
        "wait_for_clearance_count": wait_count,
        "replanned_task_count": detour_count,
        "takeover_count": 0,
        "takeover_assignments": [],
        "policy_version": POLICY_VERSION,
        "route_planner_version": "RoadGraph-Dijkstra-Topology-V1",
        "boundary": (
            "Real topology-edge route constraints plus a parameterized planned "
            "blast window; no geometric blast-radius model, CARLA blast physics, "
            "physical multi-vehicle driving or real-mine blasting data."
        ),
    })
    return result
