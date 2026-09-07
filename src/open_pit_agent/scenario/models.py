"""Simulator-independent models and contracts for reusable scenarios."""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


WORLD_STATE_SCHEMA_VERSION = "openpit.world-state.v1"
RUN_RESULT_SCHEMA_VERSION = "openpit.scenario-run-result.v1"
SCENARIO_LIFECYCLE_SCHEMA_VERSION = "openpit.scenario-lifecycle.v1"
SCENARIO_LIFECYCLE_PHASES = (
    "prepare", "start", "event", "decision", "execute", "feedback", "finish",
)


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
) -> Dict[str, Any]:
    """Attach the common V1 run contract while preserving legacy fields."""

    normalized = dict(result)
    mode = str(normalized.get("mode") or "unknown")
    status = str(normalized.get("status") or "UNKNOWN")
    task_count = int(normalized.get("task_count") or 0)
    completed = int(normalized.get("completed_task_count") or 0)
    normalized["result_schema_version"] = RUN_RESULT_SCHEMA_VERSION
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
