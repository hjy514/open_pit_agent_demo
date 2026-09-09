"""Simulator-independent models and contracts for reusable scenarios."""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


WORLD_STATE_SCHEMA_VERSION = "openpit.world-state.v1"
RUN_RESULT_SCHEMA_VERSION = "openpit.scenario-run-result.v1"
SCENARIO_SPEC_SCHEMA_VERSION = "openpit.scenario-spec.v1"
CONCRETE_EPISODE_SCHEMA_VERSION = "openpit.concrete-episode.v1"
CONCRETE_EPISODE_V2_SCHEMA_VERSION = "openpit.concrete-episode.v2"
SCENARIO_EVENT_SCHEMA_VERSION = "openpit.scenario-event.v1"
DECISION_RECORD_SCHEMA_VERSION = "openpit.decision-record.v1"
EVENT_TIMELINE_SCHEMA_VERSION = "openpit.event-timeline.v1"
DECISION_POINT_SCHEMA_VERSION = "openpit.decision-point.v1"
METRICS_SCHEMA_VERSION = "openpit.scenario-metrics.v1"
SCENARIO_LIFECYCLE_SCHEMA_VERSION = "openpit.scenario-lifecycle.v1"
SCENARIO_LIFECYCLE_PHASES = (
    "prepare", "start", "event", "decision", "execute", "feedback", "finish",
)


@dataclass(frozen=True)
class ScenarioSpec:
    """One catalog-backed format shared by every S01-S09 scenario."""

    scenario_key: str
    scenario_id: str
    name: str
    scenario_type: str
    description: str
    compatibility_config: str
    fleet: Dict[str, Any]
    randomization: List[str]
    constraints: List[str]
    events: List[str]
    success_criteria: List[str]
    termination: Any
    modes: List[str]
    carla_readiness: str
    supported_policies: List[str]
    implementation_mode: str = "unified_event_pipeline"

    @classmethod
    def from_catalog_entry(
        cls, scenario_key: str, raw: Dict[str, Any]
    ) -> "ScenarioSpec":
        key = str(scenario_key).lower()
        return cls(
            scenario_key=key,
            scenario_id=str(raw.get("scenario_id") or key),
            name=str(raw.get("name") or key),
            scenario_type=str(raw.get("type") or "generic"),
            description=str(raw.get("description") or ""),
            compatibility_config=str(raw.get("compatibility_config") or ""),
            fleet=dict(raw.get("fleet") or {}),
            randomization=list(raw.get("randomization") or []),
            constraints=list(raw.get("constraints") or []),
            events=list(raw.get("events") or []),
            success_criteria=list(raw.get("success_criteria") or []),
            termination=raw.get("termination"),
            modes=list(raw.get("modes") or []),
            carla_readiness=str(raw.get("carla_readiness") or "UNKNOWN"),
            supported_policies=list(raw.get("supported_policies") or []),
            implementation_mode=(
                "legacy_golden_compatibility_adapter"
                if key == "s08" else "unified_event_pipeline"
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCENARIO_SPEC_SCHEMA_VERSION
        return payload


@dataclass(frozen=True)
class ConcreteEpisodeV2:
    """Common V2 episode contract shared by S01-S09.

    This is deliberately a contract beside the V1 baseline, rather than a
    replacement for its execution code.  Empty or unavailable production
    resources must remain explicit; callers must not invent loaders, dump
    points or measured CARLA facts merely to fill the schema.
    """

    episode_id: str
    scenario_key: str
    scenario_id: str
    scenario_family: str
    scenario_version: str
    seed: Optional[int]
    complexity_profile: Dict[str, Any]
    map_context: Dict[str, Any]
    fleet: Dict[str, Any]
    production_system: Dict[str, Any]
    initial_state: Dict[str, Any]
    randomization: Dict[str, Any]
    event_plan: List[Dict[str, Any]]
    hard_constraints: List[Dict[str, Any]]
    admission: Dict[str, Any]
    acceptance: Dict[str, Any]
    execution_binding: Dict[str, Any]
    data_provenance: Dict[str, Any]
    implementation_status: str = "V2_CONTRACT_WITH_BASELINE_ADAPTER"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = CONCRETE_EPISODE_V2_SCHEMA_VERSION
        return payload


@dataclass
class ScenarioLifecycle:
    """Ordered lifecycle trace shared by structural and CARLA execution."""

    phases: List[Dict[str, Any]] = field(default_factory=list)

    def mark(
        self, phase: str, status: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        phase = str(phase)
        if phase not in SCENARIO_LIFECYCLE_PHASES:
            raise ValueError("unsupported scenario lifecycle phase: {}".format(phase))
        if self.phases:
            previous = self.phases[-1]["phase"]
            if SCENARIO_LIFECYCLE_PHASES.index(phase) <= (
                    SCENARIO_LIFECYCLE_PHASES.index(previous)):
                raise ValueError("scenario lifecycle phases must be ordered")
        self.phases.append({
            "sequence": len(self.phases),
            "phase": phase,
            "status": str(status),
            "evidence": dict(evidence or {}),
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCENARIO_LIFECYCLE_SCHEMA_VERSION,
            "current_phase": self.phases[-1]["phase"] if self.phases else None,
            "phases": [dict(item) for item in self.phases],
        }


@dataclass(frozen=True)
class WorldStateSnapshot:
    """Simulator-neutral state shared by scenario, decision and data layers."""

    run_id: Optional[str] = None
    tick: Optional[int] = None
    vehicles: List[Dict[str, Any]] = field(default_factory=list)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    roads: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    monitoring: Dict[str, Any] = field(default_factory=dict)
    risk: Dict[str, Any] = field(default_factory=dict)
    traffic: Dict[str, Any] = field(default_factory=dict)
    equipment: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = WORLD_STATE_SCHEMA_VERSION
        return payload


def _dict_value(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _first_dict(result: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
    for name in names:
        value = result.get(name)
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def build_unified_scenario_events(
    result: Dict[str, Any], scenario_key: str,
    scenario_spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Project scenario-specific event facts into one stable record shape."""
    compound = result.get("compound_events")
    if isinstance(compound, list) and compound:
        source_events = [dict(item) for item in compound if isinstance(item, dict)]
    else:
        payload = _first_dict(result, [
            "equipment_event", "blast_event", "weather_event",
            "congestion_event", "slope_event",
        ])
        expected_types = list(scenario_spec.get("events") or [])
        if not payload and scenario_key == "s02" and result.get("failed_vehicle_id"):
            payload = {
                "vehicle_id": result.get("failed_vehicle_id"),
                "tick": result.get("failure_tick"),
            }
        if not payload and scenario_key == "s07" and result.get("closed_edge_id"):
            payload = {
                "road_id": result.get("closed_edge_id"),
                "tick": result.get("closure_tick"),
            }
        event_type = payload.get("event_type")
        if not event_type:
            event_type = expected_types[0] if expected_types else "scenario_event"
        source_events = (
            [dict(payload, event_type=event_type)] if payload else []
        )

    normalized = []
    for index, payload in enumerate(source_events):
        event_type = str(payload.get("event_type") or payload.get("type") or "scenario_event")
        tick = payload.get("tick")
        if tick is None:
            for key in ("event_tick", "failure_tick", "blast_start_tick", "closure_tick"):
                if payload.get(key) is not None:
                    tick = payload[key]
                    break
        recovery_tick = payload.get("recovery_tick")
        if recovery_tick is None:
            recovery_tick = payload.get("clearance_tick")
        if recovery_tick is None:
            recovery_tick = result.get("recovery_tick")
        affected_vehicle_ids = []
        affected_task_ids = []
        for key in ("vehicle_id", "failed_vehicle_id"):
            if payload.get(key):
                affected_vehicle_ids.append(str(payload[key]))
        for key in ("task_id", "failed_task_id"):
            if payload.get(key):
                affected_task_ids.append(str(payload[key]))
        if not affected_vehicle_ids and result.get("failed_vehicle_id"):
            affected_vehicle_ids.append(str(result["failed_vehicle_id"]))
        if not affected_task_ids:
            for key in (
                "affected_task_ids", "route_impact_task_ids",
                "released_task_ids", "compound_affected_task_ids",
            ):
                values = result.get(key)
                if isinstance(values, list):
                    affected_task_ids.extend(str(value) for value in values)
        normalized.append({
            "schema_version": SCENARIO_EVENT_SCHEMA_VERSION,
            "sequence": index,
            "event_id": str(payload.get("event_id") or "{}-event-{:02d}".format(
                scenario_key, index + 1
            )),
            "event_type": event_type,
            "trigger": {"type": "tick", "tick": tick},
            "recovery_trigger": (
                {"type": "tick", "tick": recovery_tick}
                if recovery_tick is not None else None
            ),
            "affected_entities": {
                "vehicle_ids": sorted(set(affected_vehicle_ids)),
                "task_ids": sorted(set(affected_task_ids)),
            },
            "status": str(payload.get("status") or "APPLIED"),
            "data_origin": payload.get("data_origin") or payload.get("source"),
            "randomization": {
                "seed": result.get("seed"),
                "mode": result.get("random_mode"),
                "reproducible": result.get("seed") is not None,
            },
            "payload": payload,
        })
    return normalized


def build_event_timeline(
    events: List[Dict[str, Any]], seed: Optional[int], scenario_key: str,
) -> Dict[str, Any]:
    """Build the ordered event plan consumed by structural and CARLA modes."""
    ordered = sorted(
        (dict(item) for item in events),
        key=lambda item: (
            item.get("trigger", {}).get("tick") is None,
            item.get("trigger", {}).get("tick") or 0,
            int(item.get("sequence") or 0),
        ),
    )
    for sequence, item in enumerate(ordered):
        item["sequence"] = sequence
    ticks = [
        item.get("trigger", {}).get("tick") for item in ordered
        if item.get("trigger", {}).get("tick") is not None
    ]
    return {
        "schema_version": EVENT_TIMELINE_SCHEMA_VERSION,
        "scenario_key": str(scenario_key),
        "seed": seed,
        "reproducible": seed is not None,
        "event_count": len(ordered),
        "ordered": ticks == sorted(ticks),
        "events": ordered,
    }


def build_unified_decisions(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Combine assignment and event decisions without losing native payloads."""
    collections = [
        ("assignment", "assignments"),
        ("equipment_response", "equipment_decisions"),
        ("blast_response", "blast_decisions"),
        ("weather_response", "weather_decisions"),
        ("traffic_response", "traffic_decisions"),
        ("route_response", "route_changes"),
        ("compound_failure_response", "compound_failure_decisions"),
    ]
    records = []
    for decision_type, field_name in collections:
        values = result.get(field_name)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                continue
            task_id = item.get("task_id")
            vehicle_id = (
                item.get("selected_vehicle_id") or item.get("vehicle_id")
                or item.get("assigned_vehicle_id")
            )
            records.append({
                "schema_version": DECISION_RECORD_SCHEMA_VERSION,
                "decision_id": str(item.get("decision_id") or "{}:{}:{}".format(
                    decision_type, task_id or "none", index + 1
                )),
                "decision_type": decision_type,
                "task_id": task_id,
                "selected_vehicle_id": vehicle_id,
                "action_type": item.get("action_type") or (
                    "assign_task" if decision_type == "assignment" else decision_type
                ),
                "policy_version": item.get("policy_version") or result.get("policy_version"),
                "score": item.get("score") if item.get("score") is not None else item.get("total_cost"),
                "reason": item.get("reason") or item.get("recommendation_reason"),
                "constraint_results": item.get("constraint_results", {}),
                "candidate_evaluations": item.get("candidate_evaluations", []),
                "native_payload": dict(item),
            })
    return records


def build_decision_points(
    result: Dict[str, Any], scenario_key: str,
    events: List[Dict[str, Any]], decisions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Create operator-facing decision boundaries without claiming approval.

    Initial dispatch is auto-eligible. Event responses are marked for operator
    review in a physical run; a structural run records only that the response
    was evaluated, never that a human approved it.
    """
    mode = str(result.get("mode") or "")
    physical = mode.startswith("carla") and "check" not in mode
    assignment_decisions = [
        item for item in decisions if item.get("decision_type") == "assignment"
    ]
    response_decisions = [
        item for item in decisions if item.get("decision_type") != "assignment"
    ]
    points = []

    def point_payload(
        point_id: str, trigger: Dict[str, Any], items: List[Dict[str, Any]],
        review_policy: str, affected: Dict[str, Any], event_id: Optional[str],
    ) -> Dict[str, Any]:
        recommended = dict(items[0]) if items else None
        return {
            "schema_version": DECISION_POINT_SCHEMA_VERSION,
            "decision_point_id": point_id,
            "scenario_key": str(scenario_key),
            "trigger": dict(trigger),
            "trigger_event_id": event_id,
            "affected_entities": dict(affected),
            "candidate_actions": [dict(item) for item in items],
            "recommended_action": recommended,
            "policy_version": (
                recommended.get("policy_version") if recommended
                else result.get("policy_version")
            ),
            "confidence": (
                recommended.get("confidence") if recommended else None
            ),
            "review_policy": review_policy,
            "review_status": (
                "NOT_CAPTURED_LEGACY_EXECUTION" if physical
                and review_policy == "REQUIRED_BEFORE_EXECUTION"
                else "NOT_APPLICABLE_STRUCTURAL_SIMULATION"
                if not physical and review_policy == "REQUIRED_BEFORE_EXECUTION"
                else "NOT_REQUIRED"
            ),
            "operator_response": None,
            "execution_status": (
                "LEGACY_AUTO_EXECUTED_PENDING_GATE_UPGRADE" if physical
                and review_policy == "REQUIRED_BEFORE_EXECUTION"
                else "STRUCTURALLY_EVALUATED" if not physical
                else "READY"
            ),
            "deduplication_key": "{}:{}".format(
                scenario_key, event_id or point_id
            ),
        }

    raw_initial_assignments = result.get("initial_assignments")
    if isinstance(raw_initial_assignments, list) and raw_initial_assignments:
        initial_decisions = []
        for index, item in enumerate(raw_initial_assignments):
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            initial_decisions.append({
                "schema_version": DECISION_RECORD_SCHEMA_VERSION,
                "decision_id": "initial_assignment:{}:{}".format(
                    item["task_id"], index + 1
                ),
                "decision_type": "initial_assignment",
                "task_id": item.get("task_id"),
                "selected_vehicle_id": item.get("vehicle_id")
                    or item.get("assigned_vehicle_id"),
                "action_type": "assign_task",
                "policy_version": item.get("policy_version")
                    or result.get("policy_version"),
                "score": item.get("score"),
                "reason": item.get("reason"),
                "constraint_results": item.get("constraint_results", {}),
                "candidate_evaluations": item.get(
                    "candidate_evaluations", []
                ),
                "native_payload": dict(item),
            })
    else:
        initial_decisions = assignment_decisions

    if initial_decisions:
        points.append(point_payload(
            "{}:initial-dispatch".format(scenario_key),
            {"type": "scenario_start", "tick": 0},
            initial_decisions, "AUTO_ALLOWED", {
                "vehicle_ids": sorted(set(
                    str(item["selected_vehicle_id"])
                    for item in initial_decisions
                    if item.get("selected_vehicle_id")
                )),
                "task_ids": sorted(set(
                    str(item["task_id"]) for item in initial_decisions
                    if item.get("task_id")
                )),
            }, None,
        ))

    if events:
        # V1 binds the response collection to the event boundary. Compound
        # events retain one decision point per event while sharing the same
        # factual candidate collection until event-specific IDs are available.
        for event in events:
            affected = dict(event.get("affected_entities") or {})
            event_type = str(event.get("event_type") or "")
            event_responses = response_decisions
            if scenario_key == "s02":
                event_responses = assignment_decisions
            elif scenario_key == "s09" and event_type == "road_closure":
                event_responses = [
                    item for item in response_decisions
                    if item.get("decision_type") == "route_response"
                ]
            elif scenario_key == "s09" and "failure" in event_type:
                event_responses = [
                    item for item in response_decisions
                    if item.get("decision_type")
                    == "compound_failure_response"
                ]
            points.append(point_payload(
                "{}:{}:response".format(scenario_key, event["event_id"]),
                event.get("trigger") or {"type": "event"},
                event_responses,
                "REQUIRED_BEFORE_EXECUTION",
                affected,
                str(event["event_id"]),
            ))
    return points


def build_concrete_episode_snapshot(
    result: Dict[str, Any], scenario_key: str,
    scenario_spec: Dict[str, Any],
    scenario_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the reproducible episode envelope from factual run inputs."""
    fleet = _dict_value(result.get("fleet"))
    vehicles = fleet.get("vehicles")
    if not isinstance(vehicles, list):
        vehicles = result.get("initial_vehicle_states", [])
    resource_tasks = {
        str(item.get("task_id")): dict(item)
        for item in result.get("map_resource_task_draft", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    state_tasks = result.get("initial_task_states")
    if not isinstance(state_tasks, list):
        state_tasks = result.get("tasks", [])
    tasks = []
    task_order = list(resource_tasks)
    for item in state_tasks if isinstance(state_tasks, list) else []:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        task_id = str(item["task_id"])
        if task_id not in task_order:
            task_order.append(task_id)
        merged = resource_tasks.setdefault(task_id, {})
        merged.update(item)
    tasks = [resource_tasks[task_id] for task_id in task_order]
    mission_by_vehicle = {
        str(item.get("vehicle_id")): item
        for item in resource_tasks.values()
        if item.get("vehicle_id")
    }
    enriched_vehicles = []
    for raw in vehicles if isinstance(vehicles, list) else []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        mission = mission_by_vehicle.get(str(item.get("vehicle_id")), {})
        for key in (
            "initial_operational_state", "initial_payload_state",
            "origin_area_id", "mission_type", "spawn_point_id",
            "service_origin_point_id", "service_target_point_id",
            "deadhead_route_length_m", "deadhead_validation_status",
        ):
            if mission.get(key) is not None:
                item[key] = mission[key]
        enriched_vehicles.append(item)
    return {
        "schema_version": CONCRETE_EPISODE_SCHEMA_VERSION,
        "episode_id": result.get("run_id") or result.get("scenario_id"),
        "run_id": result.get("run_id"),
        "scenario_key": scenario_key,
        "scenario_id": result.get("scenario_id") or scenario_spec.get("scenario_id"),
        "seed": result.get("seed"),
        "generation_status": result.get("episode_generation", {}).get(
            "status", "RESOLVED_FROM_RUN_RESULT"
        ),
        "fleet": fleet,
        "vehicles": enriched_vehicles,
        "tasks": _dict_items(tasks),
        "events": [dict(item) for item in scenario_events],
    }


def build_unified_metrics(
    result: Dict[str, Any], events: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    task_count = int(result.get("task_count") or 0)
    completed = int(result.get("completed_task_count") or 0)
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "task_count": task_count,
        "completed_task_count": completed,
        "task_completion_rate": (
            completed / float(task_count) if task_count else None
        ),
        "scenario_event_count": len(events),
        "decision_count": len(decisions),
        "reassignment_count": result.get("reassignment_count"),
        "affected_task_count": result.get("affected_task_count"),
        "route_replan_count": result.get("replanned_task_count"),
        "takeover_count": result.get("takeover_count"),
        "ticks_executed": result.get("ticks_executed"),
        "execution_elapsed_seconds": result.get("execution_elapsed_seconds"),
        "measurement_status": (
            "CARLA_MEASURED" if str(result.get("mode", "")).startswith("carla")
            else "STRUCTURAL_ONLY_NO_PHYSICS"
        ),
    }


def build_world_state_snapshot(
    result: Dict[str, Any], map_context: Optional[Dict[str, Any]] = None
) -> WorldStateSnapshot:
    """Build a factual final-state view without inventing unavailable values."""

    fleet = _dict_value(result.get("fleet"))
    vehicles = result.get("final_vehicle_states")
    if not isinstance(vehicles, list):
        vehicles = fleet.get("vehicles", [])
    roads = _dict_value(result.get("road_state"))
    for key in (
        "closed_edge_id", "restricted_edge_id", "bottleneck_edge_id",
        "road_status_after_reopen", "road_status_after_recovery",
        "road_status_after_clearance",
    ):
        if result.get(key) is not None:
            roads[key] = result.get(key)
    environment = _dict_value(result.get("environment"))
    if map_context:
        environment.setdefault("map_context", dict(map_context))
    return WorldStateSnapshot(
        run_id=result.get("run_id"),
        tick=result.get("ticks_executed") or result.get("completed_tick"),
        vehicles=_dict_items(vehicles),
        tasks=_dict_items(result.get("tasks")),
        roads=roads,
        environment=environment,
        monitoring=_dict_value(result.get("monitoring")),
        risk=_dict_value(result.get("risk")),
        traffic=_dict_value(result.get("traffic")),
        equipment=_dict_value(result.get("equipment")),
    )


def normalize_scenario_run_result(
    result: Dict[str, Any], scenario_key: str,
    map_context: Optional[Dict[str, Any]] = None,
    scenario_spec: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach one S01-S09 contract while preserving compatibility fields."""

    normalized = dict(result)
    spec = dict(scenario_spec or normalized.get("scenario_spec") or {})
    if not spec:
        spec = {
            "schema_version": SCENARIO_SPEC_SCHEMA_VERSION,
            "scenario_key": str(scenario_key),
            "scenario_id": normalized.get("scenario_id"),
            "implementation_mode": "compatibility_result_projection",
        }
    scenario_events = build_unified_scenario_events(
        normalized, str(scenario_key), spec
    )
    decisions = build_unified_decisions(normalized)
    mode = str(normalized.get("mode") or "unknown")
    status = str(normalized.get("status") or "UNKNOWN")
    task_count = int(normalized.get("task_count") or 0)
    completed = int(normalized.get("completed_task_count") or 0)
    normalized["result_schema_version"] = RUN_RESULT_SCHEMA_VERSION
    normalized["scenario_spec"] = spec
    normalized["scenario_events"] = scenario_events
    event_timeline = build_event_timeline(
        scenario_events, normalized.get("seed"), str(scenario_key)
    )
    decision_points = build_decision_points(
        normalized, str(scenario_key), scenario_events, decisions
    )
    normalized["decisions"] = decisions
    normalized["event_timeline"] = event_timeline
    normalized["decision_points"] = decision_points
    normalized["task_results"] = _dict_items(normalized.get("tasks"))
    normalized["route_plans"] = _dict_items(normalized.get("route_plans"))
    normalized["generated_episode"] = build_concrete_episode_snapshot(
        normalized, str(scenario_key), spec, scenario_events
    )
    normalized["generated_episode"]["event_timeline"] = event_timeline
    # Local import keeps the data contract independent from the map-backed
    # workload generator at module import time.  V1 remains untouched and is
    # still the source consumed by the current baseline execution path.
    from .generator import build_concrete_episode_v2
    normalized["concrete_episode_v2"] = build_concrete_episode_v2(
        normalized, str(scenario_key), spec, scenario_events,
        map_context=map_context,
    ).to_dict()
    normalized["metrics"] = build_unified_metrics(
        normalized, scenario_events, decisions
    )
    normalized["data_contract"] = {
        "scenario_spec": SCENARIO_SPEC_SCHEMA_VERSION,
        "concrete_episode": CONCRETE_EPISODE_SCHEMA_VERSION,
        "concrete_episode_v2": CONCRETE_EPISODE_V2_SCHEMA_VERSION,
        "world_state": WORLD_STATE_SCHEMA_VERSION,
        "scenario_event": SCENARIO_EVENT_SCHEMA_VERSION,
        "event_timeline": EVENT_TIMELINE_SCHEMA_VERSION,
        "decision_record": DECISION_RECORD_SCHEMA_VERSION,
        "decision_point": DECISION_POINT_SCHEMA_VERSION,
        "run_result": RUN_RESULT_SCHEMA_VERSION,
        "metrics": METRICS_SCHEMA_VERSION,
    }
    normalized["run_context"] = {
        "run_id": normalized.get("run_id"),
        "scenario_key": str(scenario_key),
        "scenario_id": normalized.get("scenario_id"),
        "seed": normalized.get("seed"),
    }
    normalized["execution"] = {
        "mode": mode,
        "simulator": "carla" if mode.startswith("carla") else "structural",
        "physical_execution": mode.startswith("carla") and "check" not in mode,
        "status": status,
        "ticks_requested": normalized.get("ticks_requested"),
        "ticks_executed": normalized.get("ticks_executed"),
    }
    normalized["outcome"] = {
        "status": status,
        "task_count": task_count,
        "completed_task_count": completed,
        "all_tasks_completed": task_count > 0 and completed == task_count,
        "closed_loop_status": normalized.get("closed_loop_status"),
    }
    normalized["provenance"] = {
        "simulation_claim": normalized.get("simulation_claim"),
        "map_context": dict(map_context or {}),
        "requested_execution_policy": normalized.get(
            "requested_execution_policy"
        ),
        "policy_version": normalized.get("policy_version"),
        "route_planner_version": normalized.get("route_planner_version"),
        "risk_model_version": normalized.get("risk_model_version"),
    }
    normalized["world_state"] = build_world_state_snapshot(
        normalized, map_context=map_context
    ).to_dict()
    return normalized


@dataclass(frozen=True)
class EventTrigger:
    """A minimal V1 trigger; future triggers can add probability/region rules."""

    trigger_type: str = "tick"
    tick: Optional[int] = None
    state_path: Optional[str] = None
    state_equals: Any = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EventTrigger":
        return cls(
            trigger_type=str(raw.get("type", raw.get("trigger_type", "tick"))),
            tick=int(raw["tick"]) if raw.get("tick") is not None else None,
            state_path=(str(raw["state_path"]) if raw.get("state_path") else None),
            state_equals=raw.get("state_equals"),
        )


@dataclass(frozen=True)
class ScenarioEventSpec:
    event_id: str
    event_type: str
    trigger: EventTrigger
    impact: Dict[str, Any] = field(default_factory=dict)
    evolution: Dict[str, Any] = field(default_factory=dict)
    recovery: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], index: int = 0) -> "ScenarioEventSpec":
        trigger = raw.get("trigger", {})
        if not isinstance(trigger, dict):
            trigger = {"type": "tick", "tick": trigger}
        return cls(
            event_id=str(raw.get("event_id", "event-{}".format(index))),
            event_type=str(raw.get("event_type", raw.get("type", "scenario_event"))),
            trigger=EventTrigger.from_dict(trigger),
            impact=dict(raw.get("impact", raw.get("parameters", {})) or {}),
            evolution=dict(raw.get("evolution", {}) or {}),
            recovery=dict(raw.get("recovery", {}) or {}),
        )


@dataclass(frozen=True)
class LogicalScenario:
    scenario_id: str
    version: str
    scenario_type: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    events: List[ScenarioEventSpec] = field(default_factory=list)
    success_criteria: Dict[str, Any] = field(default_factory=dict)
    termination: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LogicalScenario":
        scenario = raw.get("scenario", raw)
        if not isinstance(scenario, dict):
            raise ValueError("scenario must be an object")
        events_raw = raw.get("events", scenario.get("events", []))
        if not isinstance(events_raw, list):
            raise ValueError("events must be a list")
        return cls(
            scenario_id=str(scenario.get("id", scenario.get("scenario_id", ""))).strip(),
            version=str(scenario.get("version", raw.get("schema_version", "1.0"))),
            scenario_type=str(scenario.get("type", "generic")),
            parameters=dict(raw.get("parameters", raw.get("task_generation", {})) or {}),
            events=[ScenarioEventSpec.from_dict(item, index) for index, item in enumerate(events_raw)],
            success_criteria=dict(raw.get("success_criteria", {}) or {}),
            termination=dict(raw.get("termination", {}) or {}),
        )

    def validate(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario.id is required")
        if not self.version:
            raise ValueError("scenario.version is required")
        seen = set()
        for event in self.events:
            if not event.event_id or event.event_id in seen:
                raise ValueError("event ids must be non-empty and unique")
            seen.add(event.event_id)
            if event.trigger.trigger_type == "tick" and event.trigger.tick is None:
                raise ValueError("tick trigger requires tick")
