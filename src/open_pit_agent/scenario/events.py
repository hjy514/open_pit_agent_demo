"""Deterministic event trigger evaluation for logical scenarios."""
from typing import Any, Dict, List

from .models import LogicalScenario, ScenarioEventSpec


def _lookup(state: Dict[str, Any], path: str) -> Any:
    value: Any = state
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class EventEngine:
    """Evaluate tick/state triggers once without mutating WorldState."""

    def __init__(self, scenario: LogicalScenario) -> None:
        scenario.validate()
        self.scenario = scenario
        self._fired = set()

    def evaluate(self, tick: int, state: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        state = state or {}
        fired: List[Dict[str, Any]] = []
        for spec in self.scenario.events:
            if spec.event_id in self._fired or not self._matches(spec, tick, state):
                continue
            self._fired.add(spec.event_id)
            fired.append({
                "event_id": spec.event_id,
                "event_type": spec.event_type,
                "tick": int(tick),
                "impact": dict(spec.impact),
                "evolution": dict(spec.evolution),
                "recovery": dict(spec.recovery),
            })
        return fired

    @staticmethod
    def _matches(spec: ScenarioEventSpec, tick: int, state: Dict[str, Any]) -> bool:
        trigger = spec.trigger
        if trigger.trigger_type == "tick":
            return trigger.tick is not None and int(tick) >= int(trigger.tick)
        if trigger.trigger_type in {"state", "state_condition"}:
            return trigger.state_path is not None and _lookup(state, trigger.state_path) == trigger.state_equals
        return False
