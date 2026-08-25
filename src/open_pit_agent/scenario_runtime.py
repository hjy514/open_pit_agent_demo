"""Resolve scenario variables into a reproducible run snapshot."""

import copy
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import ConfigError, ScenarioConfig


@dataclass(frozen=True)
class ResolvedScenario:
    scenario_id: str
    profile_version: str
    mode: str
    seed: int
    environment: Dict[str, Any]
    disaster: Dict[str, Any]
    mission: Dict[str, Any]
    realized_events: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "profile_version": self.profile_version,
            "mode": self.mode,
            "seed": self.seed,
            "environment": copy.deepcopy(self.environment),
            "disaster": copy.deepcopy(self.disaster),
            "mission": copy.deepcopy(self.mission),
            "realized_events": copy.deepcopy(self.realized_events),
        }

    def failure_plan(
        self, config: ScenarioConfig
    ) -> Tuple[str, int]:
        for event in self.realized_events:
            if (
                event.get("event_type") == "vehicle_failure"
                and event.get("triggered")
            ):
                return (
                    str(event["vehicle_id"]),
                    int(event["tick"]),
                )
        return (
            config.demo.failure_vehicle_id,
            config.demo.failure_tick,
        )


def resolve_scenario(
    config: ScenarioConfig,
    seed_override: Optional[int] = None,
    randomize: bool = False,
) -> ResolvedScenario:
    variables = copy.deepcopy(config.scenario_variables)
    randomization = variables.get("randomization", {})
    if not isinstance(randomization, dict):
        raise ConfigError(
            "scenario_variables.randomization must be an object"
        )

    configured_seed = int(
        randomization.get("seed", config.demo.random_seed)
    )
    if seed_override is not None:
        seed = int(seed_override)
        mode = "seed_override"
    elif randomize:
        seed = random.SystemRandom().randint(1, 2147483647)
        mode = "random"
    else:
        seed = configured_seed
        mode = str(randomization.get("mode", "fixed"))

    rng = random.Random(seed)
    configured_events = randomization.get("events", [])
    if not isinstance(configured_events, list):
        raise ConfigError(
            "scenario_variables.randomization.events must be a list"
        )

    realized_events = []
    vehicle_ids = {
        vehicle.vehicle_id for vehicle in config.vehicles
    }
    for index, item in enumerate(configured_events):
        if not isinstance(item, dict):
            raise ConfigError(
                "random event {} must be an object".format(index)
            )
        event_type = str(item.get("event_type", "")).strip()
        event_id = str(
            item.get("event_id", "event-{}".format(index))
        )
        if not event_type:
            raise ConfigError(
                "random event {} requires event_type".format(event_id)
            )
        probability = float(item.get("probability", 1.0))
        if probability < 0.0 or probability > 1.0:
            raise ConfigError(
                "random event {} probability must be 0..1".format(
                    event_id
                )
            )
        enabled = bool(item.get("enabled", True))
        triggered = enabled and rng.random() <= probability
        resolved = {
            "event_id": event_id,
            "event_type": event_type,
            "enabled": enabled,
            "probability": probability,
            "triggered": triggered,
        }
        if event_type == "vehicle_failure":
            candidates = [
                str(value)
                for value in item.get(
                    "candidate_vehicle_ids",
                    [config.demo.failure_vehicle_id],
                )
            ]
            if not candidates or not set(candidates).issubset(
                vehicle_ids
            ):
                raise ConfigError(
                    "random event {} has invalid failure candidates".format(
                        event_id
                    )
                )
            tick_range = item.get(
                "tick_range",
                [
                    config.demo.failure_tick,
                    config.demo.failure_tick,
                ],
            )
            if (
                not isinstance(tick_range, list)
                or len(tick_range) != 2
            ):
                raise ConfigError(
                    "random event {} tick_range must be [min,max]".format(
                        event_id
                    )
                )
            tick_min, tick_max = (
                int(tick_range[0]),
                int(tick_range[1]),
            )
            if tick_min < 0 or tick_max < tick_min:
                raise ConfigError(
                    "random event {} has invalid tick_range".format(
                        event_id
                    )
                )
            resolved.update(
                {
                    "vehicle_id": rng.choice(candidates),
                    "tick": rng.randint(tick_min, tick_max),
                }
            )
        else:
            resolved["parameters"] = copy.deepcopy(
                item.get("parameters", {})
            )
        realized_events.append(resolved)

    return ResolvedScenario(
        scenario_id=config.scenario_id,
        profile_version=str(
            variables.get("profile_version", "1.0-compatible")
        ),
        mode=mode,
        seed=seed,
        environment=dict(variables.get("environment", {})),
        disaster=dict(variables.get("disaster", {})),
        mission=dict(variables.get("mission", {})),
        realized_events=realized_events,
    )
