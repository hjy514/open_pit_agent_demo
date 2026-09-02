"""Simulator-independent models for reusable logical scenarios."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
