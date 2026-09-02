"""Deterministically resolve a scenario config into one concrete episode.

This module has no CARLA, scheduler, API or SQLite dependency.  It is the
stable boundary between a reusable logical scenario template and the exact
vehicle/task/event inputs used by one run.
"""

from dataclasses import asdict, dataclass
from random import Random
from typing import Any, Dict, List, Optional, Tuple

from ..config import ScenarioConfig


@dataclass(frozen=True)
class EpisodeVehicle:
    vehicle_id: str
    display_name: str
    equipment_type: str
    blueprint: str
    capabilities: List[str]
    spawn_point_index: int
    initial_role: str
    initial_status: str
    available: bool
    active: bool
    in_traffic: bool


@dataclass(frozen=True)
class EpisodeTask:
    task_id: str
    zone_id: str
    task_type: str
    priority: int
    required_capabilities: List[str]
    preferred_vehicle_id: Optional[str]


@dataclass(frozen=True)
class EpisodeEvent:
    event_id: str
    event_type: str
    trigger_tick: int
    target_vehicle_id: Optional[str]
    parameters: Dict[str, Any]


@dataclass(frozen=True)
class ConcreteEpisode:
    """Fully specified, reproducible input snapshot for exactly one run."""

    run_id: str
    scenario_id: str
    scenario_name: str
    scenario_version: str
    seed: int
    fleet_snapshot: Dict[str, Any]
    vehicles: List[EpisodeVehicle]
    tasks: List[EpisodeTask]
    events: List[EpisodeEvent]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _choose_ids(
    randomizer: Random, vehicle_ids: List[str], count: int
) -> List[str]:
    """Choose a deterministic subset while preserving a readable output order."""

    if count >= len(vehicle_ids):
        return list(vehicle_ids)
    shuffled = list(vehicle_ids)
    randomizer.shuffle(shuffled)
    selected = set(shuffled[:count])
    return [vehicle_id for vehicle_id in vehicle_ids if vehicle_id in selected]


def _resolve_roles(config: ScenarioConfig, randomizer: Random) -> Dict[str, str]:
    """Resolve runtime roles without mutating static vehicle definitions."""

    vehicles = list(config.vehicles)
    if config.fleet.role_policy == "fixed":
        return {item.vehicle_id: item.role_name for item in vehicles}

    roles: List[str] = []
    for role, count in sorted(config.fleet.role_counts.items()):
        roles.extend([role] * count)
    roles.extend(["unassigned"] * (len(vehicles) - len(roles)))
    randomizer.shuffle(roles)
    return {
        item.vehicle_id: roles[index]
        for index, item in enumerate(vehicles)
    }


def build_episode(
    config: ScenarioConfig,
    run_id: str,
    seed: Optional[int] = None,
    realized_events: Optional[List[Dict[str, Any]]] = None,
    failure_plan: Optional[Tuple[str, int]] = None,
) -> ConcreteEpisode:
    """Resolve one deterministic episode from the currently configured fleet.

    V1 intentionally does not create additional vehicle definitions or task
    instances.  It snapshots the configured vehicles/zones and applies only
    configured fleet availability/role metadata.  CARLA execution remains
    untouched until a later integration phase.
    """

    effective_seed = config.demo.random_seed if seed is None else int(seed)
    randomizer = Random(effective_seed)
    vehicle_ids = [item.vehicle_id for item in config.vehicles]
    available_ids = set(
        _choose_ids(
            randomizer, vehicle_ids, config.fleet.available_vehicles
        )
    )
    active_ids = set(
        _choose_ids(
            randomizer,
            [item for item in vehicle_ids if item in available_ids],
            config.fleet.active_vehicles,
        )
    )
    traffic_ids = set(
        _choose_ids(
            randomizer,
            [item for item in vehicle_ids if item in active_ids],
            config.fleet.traffic_vehicles,
        )
    )
    roles = _resolve_roles(config, randomizer)

    vehicles = []
    for item in config.vehicles:
        available = item.vehicle_id in available_ids
        active = item.vehicle_id in active_ids
        in_traffic = item.vehicle_id in traffic_ids
        status = (
            "unavailable"
            if not available
            else "in_traffic"
            if in_traffic
            else "active"
            if active
            else "idle"
        )
        vehicles.append(
            EpisodeVehicle(
                vehicle_id=item.vehicle_id,
                display_name=item.display_name,
                equipment_type=item.equipment_type,
                blueprint=item.blueprint,
                capabilities=list(item.capabilities),
                spawn_point_index=item.spawn_point_index,
                initial_role=roles[item.vehicle_id],
                initial_status=status,
                available=available,
                active=active,
                in_traffic=in_traffic,
            )
        )

    tasks = [
        EpisodeTask(
            task_id="initial:{}".format(zone.zone_id),
            zone_id=zone.zone_id,
            task_type="inspection",
            priority=zone.priority,
            required_capabilities=list(zone.required_capabilities),
            preferred_vehicle_id=zone.preferred_vehicle_id,
        )
        for zone in config.zones
        if zone.initial_task
    ]
    events = []
    for index, item in enumerate(realized_events or []):
        if not isinstance(item, dict) or not item.get("triggered", False):
            continue
        event_type = str(item.get("event_type", "scenario_event"))
        target_vehicle_id = item.get("vehicle_id")
        trigger_tick = int(item.get("tick", 0))
        events.append(
            EpisodeEvent(
                event_id=str(item.get("event_id", "event-{}".format(index))),
                event_type=event_type,
                trigger_tick=trigger_tick,
                target_vehicle_id=(
                    str(target_vehicle_id) if target_vehicle_id else None
                ),
                parameters={
                    "source": "resolved_scenario",
                    "configured_probability": item.get("probability"),
                    "parameters": dict(item.get("parameters", {})),
                },
            )
        )
    if (
        config.demo.failure_enabled
        and not any(item.event_type == "vehicle_failure" for item in events)
    ):
        failure_vehicle_id, failure_tick = failure_plan or (
            config.demo.failure_vehicle_id,
            config.demo.failure_tick,
        )
        events.append(
            EpisodeEvent(
                event_id="configured_failure:{}".format(failure_vehicle_id),
                event_type="vehicle_failure",
                trigger_tick=int(failure_tick),
                target_vehicle_id=str(failure_vehicle_id),
                parameters={"source": "resolved_scenario_fallback"},
            )
        )

    fleet_snapshot = {
        "total_vehicles": config.fleet.total_vehicles,
        "available_vehicles": config.fleet.available_vehicles,
        "active_vehicles": config.fleet.active_vehicles,
        "traffic_vehicles": config.fleet.traffic_vehicles,
        "role_policy": config.fleet.role_policy,
        "role_counts": dict(config.fleet.role_counts),
        "task_load": config.fleet.task_load,
        "traffic_density": config.fleet.traffic_density,
    }
    return ConcreteEpisode(
        run_id=str(run_id),
        scenario_id=config.scenario_id,
        scenario_name=config.scenario_name,
        scenario_version=config.schema_version,
        seed=effective_seed,
        fleet_snapshot=fleet_snapshot,
        vehicles=vehicles,
        tasks=tasks,
        events=events,
    )
