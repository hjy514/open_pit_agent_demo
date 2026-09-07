"""Scenario template and concrete-episode utilities."""

from .episode import ConcreteEpisode, build_episode
from .models import (
    RUN_RESULT_SCHEMA_VERSION, SCENARIO_LIFECYCLE_SCHEMA_VERSION,
    WORLD_STATE_SCHEMA_VERSION, EventTrigger, LogicalScenario,
    ScenarioEventSpec, ScenarioLifecycle, WorldStateSnapshot,
    build_world_state_snapshot, normalize_scenario_run_result,
)
from .loader import load_logical_scenario
from .catalog import (
    compatibility_config_path, load_scenario_catalog,
    selectable_vehicle_counts, validate_scenario_request,
)
from .events import EventEngine
from .fleet import (
    FleetSnapshot, EpisodeVehicle as FleetEpisodeVehicle, VehicleMaster,
    resolve_fleet, snapshot_from_episode, vehicle_state_snapshot,
)
from .mock_runner import run_s01_structural_mock
from .s02_runner import run_random_s02_structural_mock, run_s02_structural_mock
from .s03_runner import run_random_s03_structural_mock
from .s04_runner import run_random_s04_structural_mock
from .s05_runner import run_random_s05_structural_mock
from .s06_runner import run_random_s06_structural_mock
from .s07_runner import run_random_s07_structural_mock, run_s07_structural_mock
from .s09_runner import run_random_s09_structural_mock
from .resource_admission import ScenarioResourceAdmission, admit_scenario_resources
from .operating_areas import load_operating_area_profile, register_operating_area_profile
from .runner import (
    SCENARIO_CATALOG, SUPPORTED_STRUCTURAL_SCENARIOS, run_structural_scenario,
    summarize_structural_batch, validate_structural_closed_loop,
)
from .carla_execution import (
    CARLA_SCENARIOS, run_carla_scenario_execution, run_s01_carla_execution,
)
from .random_s01 import run_random_s01_structural_mock

__all__ = [
    "ConcreteEpisode", "build_episode", "EventTrigger", "LogicalScenario",
    "ScenarioEventSpec", "WorldStateSnapshot", "build_world_state_snapshot",
    "normalize_scenario_run_result", "RUN_RESULT_SCHEMA_VERSION",
    "SCENARIO_LIFECYCLE_SCHEMA_VERSION", "WORLD_STATE_SCHEMA_VERSION",
    "ScenarioLifecycle", "load_logical_scenario", "EventEngine",
    "load_scenario_catalog", "compatibility_config_path",
    "selectable_vehicle_counts", "validate_scenario_request",
    "VehicleMaster", "FleetEpisodeVehicle", "FleetSnapshot", "resolve_fleet", "vehicle_state_snapshot",
    "snapshot_from_episode", "run_s01_structural_mock", "run_s02_structural_mock",
    "run_random_s02_structural_mock", "run_s07_structural_mock",
    "run_random_s03_structural_mock",
    "run_random_s04_structural_mock",
    "run_random_s05_structural_mock",
    "run_random_s06_structural_mock",
    "run_random_s07_structural_mock",
    "run_random_s09_structural_mock",
    "ScenarioResourceAdmission", "admit_scenario_resources",
    "load_operating_area_profile", "register_operating_area_profile",
    "SCENARIO_CATALOG", "SUPPORTED_STRUCTURAL_SCENARIOS", "run_structural_scenario",
    "summarize_structural_batch",
    "validate_structural_closed_loop",
    "run_random_s01_structural_mock",
    "run_s01_carla_execution",
    "run_carla_scenario_execution",
    "CARLA_SCENARIOS",
]
