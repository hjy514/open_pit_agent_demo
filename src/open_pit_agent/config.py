"""JSON configuration loading and validation."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .models import Position


class ConfigError(ValueError):
    """Raised when a scenario configuration is incomplete or inconsistent."""


@dataclass(frozen=True)
class CarlaConfig:
    root: Path
    host: str
    port: int
    timeout_seconds: float
    map_name: str


@dataclass(frozen=True)
class VehicleConfig:
    vehicle_id: str
    display_name: str
    equipment_type: str
    role_name: str
    blueprint: str
    spawn_point_index: int
    mock_position: Position
    target_speed_kmh: float
    capabilities: List[str]


@dataclass(frozen=True)
class ZoneConfig:
    zone_id: str
    display_name: str
    priority: int
    required_capabilities: List[str]
    target_spawn_point_index: int
    mock_position: Position
    initial_task: bool = True


@dataclass(frozen=True)
class DemoOptions:
    failure_vehicle_id: str
    failure_tick: int
    random_seed: int
    arrival_tolerance_m: float
    task_timeout_ticks: int
    fault_pull_over_offset_m: float
    idle_pull_over_offset_m: float


@dataclass(frozen=True)
class ScenarioConfig:
    schema_version: str
    scenario_id: str
    scenario_name: str
    instruction: str
    carla: CarlaConfig
    vehicles: List[VehicleConfig]
    zones: List[ZoneConfig]
    demo: DemoOptions
    scenario_variables: Dict[str, Any]


def _position(value: Dict[str, Any], label: str) -> Position:
    try:
        return Position(
            x=float(value["x"]),
            y=float(value["y"]),
            z=float(value.get("z", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("{} must contain numeric x/y/z values".format(label)) from exc


def load_config(path: Path) -> ScenarioConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError("Configuration file not found: {}".format(config_path)) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("Invalid JSON in {}: {}".format(config_path, exc)) from exc

    required_top_level = {
        "schema_version",
        "scenario_id",
        "scenario_name",
        "instruction",
        "carla",
        "vehicles",
        "zones",
        "demo",
    }
    missing = sorted(required_top_level.difference(raw))
    if missing:
        raise ConfigError("Missing top-level fields: {}".format(", ".join(missing)))

    carla_raw = raw["carla"]
    carla_root = os.environ.get(
        "OPENPIT_CARLA_ROOT",
        str(carla_raw["root"]),
    )
    carla = CarlaConfig(
        root=Path(carla_root).expanduser(),
        host=str(carla_raw["host"]),
        port=int(carla_raw["port"]),
        timeout_seconds=float(carla_raw["timeout_seconds"]),
        map_name=str(carla_raw["map_name"]),
    )

    vehicles = []
    for index, item in enumerate(raw["vehicles"]):
        vehicles.append(
            VehicleConfig(
                vehicle_id=str(item["vehicle_id"]),
                display_name=str(item["display_name"]),
                equipment_type=str(item["equipment_type"]),
                role_name=str(item["role_name"]),
                blueprint=str(item["blueprint"]),
                spawn_point_index=int(item["spawn_point_index"]),
                mock_position=_position(
                    item["mock_position"], "vehicles[{}].mock_position".format(index)
                ),
                target_speed_kmh=float(item["target_speed_kmh"]),
                capabilities=[str(value) for value in item["capabilities"]],
            )
        )

    zones = []
    for index, item in enumerate(raw["zones"]):
        zones.append(
            ZoneConfig(
                zone_id=str(item["zone_id"]),
                display_name=str(item["display_name"]),
                priority=int(item["priority"]),
                required_capabilities=[
                    str(value) for value in item["required_capabilities"]
                ],
                target_spawn_point_index=int(item["target_spawn_point_index"]),
                mock_position=_position(
                    item["mock_position"], "zones[{}].mock_position".format(index)
                ),
                initial_task=bool(item.get("initial_task", True)),
            )
        )

    demo_raw = raw["demo"]
    result = ScenarioConfig(
        schema_version=str(raw["schema_version"]),
        scenario_id=str(raw["scenario_id"]),
        scenario_name=str(raw["scenario_name"]),
        instruction=str(raw["instruction"]),
        carla=carla,
        vehicles=vehicles,
        zones=zones,
        demo=DemoOptions(
            failure_vehicle_id=str(demo_raw["failure_vehicle_id"]),
            failure_tick=int(demo_raw["failure_tick"]),
            random_seed=int(demo_raw["random_seed"]),
            arrival_tolerance_m=float(demo_raw["arrival_tolerance_m"]),
            task_timeout_ticks=int(demo_raw["task_timeout_ticks"]),
            fault_pull_over_offset_m=float(
                demo_raw.get("fault_pull_over_offset_m", 0.0)
            ),
            idle_pull_over_offset_m=float(
                demo_raw.get("idle_pull_over_offset_m", 0.0)
            ),
        ),
        scenario_variables=dict(raw.get("scenario_variables", {})),
    )
    _validate(result)
    return result


def _validate(config: ScenarioConfig) -> None:
    if not config.vehicles:
        raise ConfigError("At least one vehicle is required")
    if not config.zones:
        raise ConfigError("At least one zone is required")

    vehicle_ids = [item.vehicle_id for item in config.vehicles]
    role_names = [item.role_name for item in config.vehicles]
    zone_ids = [item.zone_id for item in config.zones]
    if len(vehicle_ids) != len(set(vehicle_ids)):
        raise ConfigError("vehicle_id values must be unique")
    if len(role_names) != len(set(role_names)):
        raise ConfigError("role_name values must be unique")
    if len(zone_ids) != len(set(zone_ids)):
        raise ConfigError("zone_id values must be unique")
    if config.demo.failure_vehicle_id not in set(vehicle_ids):
        raise ConfigError("failure_vehicle_id must reference a configured vehicle")
    if config.demo.arrival_tolerance_m <= 0:
        raise ConfigError("arrival_tolerance_m must be greater than zero")
    if config.demo.task_timeout_ticks <= 0:
        raise ConfigError("task_timeout_ticks must be greater than zero")
    if config.demo.fault_pull_over_offset_m < 0:
        raise ConfigError("fault_pull_over_offset_m cannot be negative")
    if config.demo.idle_pull_over_offset_m < 0:
        raise ConfigError("idle_pull_over_offset_m cannot be negative")
    if not isinstance(config.scenario_variables, dict):
        raise ConfigError("scenario_variables must be an object")

    emergency = config.scenario_variables.get("emergency_event")
    if emergency is not None:
        if not isinstance(emergency, dict):
            raise ConfigError(
                "scenario_variables.emergency_event must be an object"
            )
        safe_route = emergency.get("safe_route")
        if safe_route is not None:
            if not isinstance(safe_route, dict):
                raise ConfigError(
                    "emergency_event.safe_route must be an object"
                )
            if not str(safe_route.get("route_plan_id", "")).strip():
                raise ConfigError(
                    "emergency_event.safe_route requires route_plan_id"
                )
            if int(safe_route.get("waypoint_spawn_point_index", -1)) < 0:
                raise ConfigError(
                    "safe route waypoint_spawn_point_index cannot be negative"
                )
            task_types = safe_route.get("task_types", [])
            if not isinstance(task_types, list) or not task_types:
                raise ConfigError(
                    "emergency_event.safe_route task_types must be a non-empty list"
                )

    all_capabilities = {
        capability
        for vehicle in config.vehicles
        for capability in vehicle.capabilities
    }
    for zone in config.zones:
        missing = set(zone.required_capabilities).difference(all_capabilities)
        if missing:
            raise ConfigError(
                "Zone {} requires unavailable capabilities: {}".format(
                    zone.zone_id, ", ".join(sorted(missing))
                )
            )
