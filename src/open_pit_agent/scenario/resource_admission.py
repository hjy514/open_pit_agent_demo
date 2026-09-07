"""Deterministic admission of map resources for multi-vehicle scenarios.

This module is deliberately read-only.  It selects spawn points from facts
already stored in ``map_resources.db`` and reports route-validation evidence;
it neither creates CARLA actors nor claims unverified routes are drivable.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
from random import Random
from typing import Any, Dict, List, Optional

from ..map_resources import MapResourceStore


@dataclass(frozen=True)
class ScenarioResourceAdmission:
    scenario_id: str
    seed: int
    execution_mode: str
    map_id: str
    resource_version: str
    requested_vehicle_count: int
    selected_spawn_points: List[Dict[str, object]]
    blocked_pair_count: int
    physical_routes_reached: List[Dict[str, object]]
    physical_routes_rejected: List[Dict[str, object]]
    limitations: List[str]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _pair_key(first: str, second: str):
    return tuple(sorted((str(first), str(second))))


def admit_scenario_resources(
    config: Any,
    database_path: Optional[Path] = None,
    seed: Optional[int] = None,
    execution_mode: str = "structural_mock",
) -> ScenarioResourceAdmission:
    """Choose a reproducible, non-conflicting-on-record spawn subset.

    ``structural_mock`` may use any P3-verified spawn point not recorded as a
    dual-spawn failure with an already selected point.  ``carla_validation``
    still only reports the isolated P6 route evidence; it does not authorise a
    fleet run.
    """
    if execution_mode not in {"structural_mock", "carla_validation"}:
        raise ValueError("unsupported execution_mode: {}".format(execution_mode))
    binding = getattr(config, "map_resource", None)
    if binding is None or not binding.map_id or not binding.resource_version:
        raise ValueError("scenario requires map_resource map_id and resource_version")
    resolved_database = database_path or binding.database_path
    if resolved_database is None:
        raise ValueError("scenario requires map_resource.database_path")

    effective_seed = int(config.demo.random_seed if seed is None else seed)
    requested = int(config.fleet.total_vehicles)
    with MapResourceStore(Path(resolved_database)) as store:
        spawn_points = list(store.verified_spawn_points(binding.map_id))
        blocked_pairs = set(store.blocked_dual_spawn_pairs(
            binding.map_id, binding.resource_version
        ))
        route_facts = list(store.physical_route_validations(
            binding.map_id, binding.resource_version
        ))

    randomizer = Random(effective_seed)
    candidates = list(spawn_points)
    randomizer.shuffle(candidates)
    selected = []
    for point in candidates:
        if all(
            _pair_key(point["point_id"], chosen["point_id"]) not in blocked_pairs
            for chosen in selected
        ):
            selected.append(point)
        if len(selected) == requested:
            break
    if len(selected) != requested:
        raise ValueError(
            "insufficient admissible spawn points: requested={}, selected={}".format(
                requested, len(selected)
            )
        )

    reached = [
        item for item in route_facts
        if item["validation_status"] == "PHYSICAL_REACHED"
    ]
    rejected = [
        item for item in route_facts
        if item["validation_status"] != "PHYSICAL_REACHED"
    ]
    return ScenarioResourceAdmission(
        scenario_id=str(config.scenario_id),
        seed=effective_seed,
        execution_mode=execution_mode,
        map_id=str(binding.map_id),
        resource_version=str(binding.resource_version),
        requested_vehicle_count=requested,
        selected_spawn_points=selected,
        blocked_pair_count=len(blocked_pairs),
        physical_routes_reached=reached,
        physical_routes_rejected=rejected,
        limitations=[
            "Spawn selection uses P3 verified points and excludes only recorded DUAL_SPAWN_BLOCKED pairs.",
            "PHYSICAL_REACHED routes are isolated single-truck evidence, not multi-vehicle collision or clearance evidence.",
            "This admission result does not create CARLA actors or authorise a multi-vehicle CARLA run.",
        ],
    )
