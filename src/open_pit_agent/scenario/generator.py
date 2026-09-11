"""Seeded, map-constrained episode workload generation.

This module is the single construction boundary between static map facts and
runtime scenario workloads.  It does not assert fleet-safety: callers choose
whether its input routes are P5 planner facts or the narrower P6 single-truck
facts required for CARLA execution.
"""
from dataclasses import replace
from math import ceil
from random import Random
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..config import VehicleConfig, ZoneConfig
from ..map_resources import MapResourceStore
from ..map_resources.route_coverage import constrained_seeded_tasks, fleet_roles
from ..models import Position, Task
from .models import ConcreteEpisodeV2


ROLE_DETAILS = {
    "haul": ("运输车", "haul_truck", ["haul", "inspection"], ["haul"]),
    "inspection": ("巡检车", "inspection_vehicle", ["inspection", "slope_monitoring"], ["inspection", "slope_monitoring"]),
    # The common fleet keeps one multi-role response truck.  Its portable
    # monitoring capability provides capability-safe recovery capacity after
    # an inspection truck fails; it does not bypass scheduler constraints.
    "support": (
        "保障车", "support_vehicle",
        ["inspection", "slope_monitoring", "emergency_support"],
        ["emergency_support"],
    ),
}


class EpisodeGenerationError(ValueError):
    """A requested seeded episode cannot meet its hard admission rules."""


EVENT_TIMING_SALTS = {
    "s02": 20020, "s03": 30030, "s04": 40040, "s05": 50050,
    "s06": 60060, "s07": 70070, "s09": 90090,
}


PRODUCTION_CYCLE_STAGES = (
    "idle", "to_loader", "queue_loader", "loading", "loaded_haul",
    "event_hold", "task_handover", "queue_dump", "dumping", "returning",
    "terminal",
)

PRODUCTION_RUNTIME_SCHEMA_VERSION = "openpit.production-runtime.v1"

STRUCTURAL_PRODUCTION_PARAMETERS = {
    "empty_speed_kmh": 20.0,
    "loaded_speed_kmh": 15.0,
    "loading_service_ticks": 40,
    "dumping_service_ticks": 20,
    "handover_service_ticks": 15,
    "tick_seconds": 1.0,
    "source": "ENGINEERING_SURROGATE_NOT_CALIBRATED_MINE_DATA",
}


EVENT_CONTRACTS = {
    "vehicle_failure": {
        "semantic_trigger": "assigned vehicle is executing and health changes to failed",
        "recovery_condition": "released task reaches a legal terminal state after reassignment",
    },
    "loading_equipment_failure": {
        "semantic_trigger": "active loader becomes unavailable while demand is assigned",
        "recovery_condition": "affected work is retargeted or safely held until loader recovery",
    },
    "blasting_control": {
        "semantic_trigger": "planned blast enters notice and exclusion-window sequence",
        "recovery_condition": "clearance is recorded before affected work resumes",
    },
    "extreme_weather": {
        "semantic_trigger": "weather severity crosses the configured operating threshold",
        "recovery_condition": "weather capacity restriction is removed or run ends safely",
    },
    "congestion": {
        "semantic_trigger": "route occupancy or queue delay crosses the configured threshold",
        "recovery_condition": "traffic pressure clears or affected work is safely rescheduled",
    },
    "road_closure": {
        "semantic_trigger": "an in-use road edge changes to CLOSED",
        "recovery_condition": "affected work is replanned, held, or road state returns OPEN",
    },
    "progressive_slope_risk": {
        "semantic_trigger": "risk assessment progresses to the configured intervention level",
        "recovery_condition": "human-reviewed response and affected-task disposition are recorded",
    },
}

EVENT_TYPE_ALIASES = {
    "blasting_control": ("blasting_control", "planned_blasting_temporary_control"),
    "extreme_weather": (
        "extreme_weather", "extreme_rainfall_road_capacity_degradation",
    ),
    "congestion": ("congestion", "shared_road_capacity_degradation"),
}


MISSION_PROFILES = {
    "haul": {
        "task_type": "haul_transport",
        "mission_type": "load_haul_dump",
        "origin_area_types": {"haul_loading"},
        "target_area_types": {"haul_dump"},
        "initial_operational_state": "at_loading_area_ready",
    },
    "inspection": {
        "task_type": "slope_inspection",
        "mission_type": "inspection_patrol",
        "origin_area_types": set(),
        "target_area_types": {"inspection_task_core"},
        "initial_operational_state": "inspection_ready",
    },
    "support": {
        "task_type": "equipment_support",
        "mission_type": "support_response",
        "origin_area_types": set(),
        "target_area_types": {"support_task_core", "safe_wait"},
        "initial_operational_state": "support_ready",
    },
}


def _area_index(areas: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    indexed: Dict[str, List[Dict[str, Any]]] = {}
    for area in areas:
        if not isinstance(area, dict) or area.get("selection_mode") == "not_selectable":
            continue
        for point_id in area.get("point_ids", []):
            indexed.setdefault(str(point_id), []).append(dict(area))
    return indexed


def _role_area(area: Dict[str, Any], role: str, area_types: Set[str]) -> bool:
    if str(area.get("area_type")) not in area_types:
        return False
    allowed = {str(item) for item in area.get("allowed_roles", [])}
    aliases = {
        "haul": {"haul"},
        "inspection": {"inspection", "risk_monitoring"},
        "support": {"support", "emergency_support"},
    }[role]
    return bool(allowed.intersection(aliases))


def _matching_area(point_id: str, role: str, area_types: Set[str],
                   indexed: Dict[str, List[Dict[str, Any]]]
                   ) -> Optional[Dict[str, Any]]:
    matches = [
        item for item in indexed.get(str(point_id), [])
        if _role_area(item, role, area_types)
    ]
    return sorted(matches, key=lambda item: str(item.get("area_id")))[0] if matches else None


def _route_matches_mission(route: Dict[str, Any], role: str,
                           indexed: Dict[str, List[Dict[str, Any]]]) -> bool:
    profile = MISSION_PROFILES[role]
    if _matching_area(
        str(route["to_point_id"]), role,
        profile["target_area_types"], indexed,
    ) is None:
        return False
    required_origins = profile["origin_area_types"]
    return not required_origins or _matching_area(
        str(route["from_point_id"]), role, required_origins, indexed,
    ) is not None


def _semantic_seeded_tasks(
    routes: Iterable[Dict[str, Any]], blocked_pairs: Iterable[Tuple[str, str]],
    seed: int, vehicle_count: int, minimum_length_m: float,
    maximum_length_m: float, scenario_key: str,
    operating_areas: Iterable[Dict[str, Any]], avoid_pairs=None,
) -> List[Dict[str, Any]]:
    """Choose routes after role/mission semantics have been resolved.

    This is a constrained sampler, not a mine-production optimizer.  It keeps
    random initial points reproducible while preventing an inspection or haul
    task from receiving an unrelated arbitrary destination.
    """
    roles = fleet_roles(vehicle_count)
    indexed = _area_index(operating_areas)
    blocked = {
        tuple(sorted((str(a), str(b)))) for a, b in blocked_pairs
    }
    blocked.update(
        tuple(sorted((str(a), str(b)))) for a, b in (avoid_pairs or set())
    )
    eligible = [
        dict(item) for item in routes
        if _within_window(item, minimum_length_m, maximum_length_m)
    ]
    # Keep the RNG call order stable across Python processes.  Iterating a
    # set here made an identical Scenario+Seed produce different workloads
    # when hash randomisation changed the role order.
    unique_roles = list(dict.fromkeys(roles))
    by_role = {
        role: [item for item in eligible if _route_matches_mission(item, role, indexed)]
        for role in unique_roles
    }
    if any(not by_role[role] for role in roles):
        raise EpisodeGenerationError("SEMANTIC_AREA_ROUTE_COVERAGE_INSUFFICIENT")

    randomizer = Random(int(seed) + 17017)
    for _attempt in range(96):
        candidates = {role: list(items) for role, items in by_role.items()}
        for items in candidates.values():
            randomizer.shuffle(items)
        selected: List[Dict[str, Any]] = []
        origins: Set[str] = set()
        destinations: Set[str] = set()
        for index, role in enumerate(roles):
            route = next((
                item for item in candidates[role]
                if str(item["from_point_id"]) not in origins
                and str(item["to_point_id"]) not in destinations
                and str(item["from_point_id"]) not in destinations
                and str(item["to_point_id"]) not in origins
                and not _blocked(str(item["from_point_id"]), origins, blocked)
            ), None)
            if route is None:
                break
            origin = str(route["from_point_id"])
            destination = str(route["to_point_id"])
            selected.append({
                "task_id": "{}-seed-{}-task-{:02d}".format(
                    str(scenario_key).lower(), seed, index + 1
                ),
                "vehicle_id": "{}_vehicle_{:02d}".format(role, index + 1),
                "vehicle_role": role,
                "vehicle_slot": index + 1,
                **route,
            })
            origins.add(origin)
            destinations.add(destination)
        if len(selected) == int(vehicle_count):
            return selected
    raise EpisodeGenerationError("SEMANTIC_AREA_ROUTE_COMBINATION_INSUFFICIENT")


def _annotate_task_semantics(tasks: Iterable[Dict[str, Any]],
                             operating_areas: Iterable[Dict[str, Any]],
                             selection_status: str) -> List[Dict[str, Any]]:
    indexed = _area_index(operating_areas)
    output = []
    for raw in tasks:
        item = dict(raw)
        role = str(item["vehicle_role"])
        profile = MISSION_PROFILES[role]
        origin_area = _matching_area(
            str(item["from_point_id"]), role,
            profile["origin_area_types"], indexed,
        ) if profile["origin_area_types"] else None
        target_area = _matching_area(
            str(item["to_point_id"]), role,
            profile["target_area_types"], indexed,
        )
        item.update({
            "task_type": profile["task_type"],
            "mission_type": profile["mission_type"],
            "initial_operational_state": (
                profile["initial_operational_state"]
                if role != "haul" or origin_area is not None
                else "at_parameterized_loading_anchor"
            ),
            "initial_payload_state": "empty" if role == "haul" else "not_applicable",
            "origin_area_id": origin_area.get("area_id") if origin_area else None,
            "origin_area_type": origin_area.get("area_type") if origin_area else None,
            "target_area_id": target_area.get("area_id") if target_area else None,
            "target_area_type": target_area.get("area_type") if target_area else None,
            "target_area_display_name": (
                target_area.get("display_name") if target_area else None
            ),
            "semantic_selection_status": selection_status,
            "scenario_data_origin": "SEEDED_ENGINEERING_SCENARIO_NOT_REAL_MINE_DATA",
        })
        output.append(item)
    return output


def _assign_deadhead_staging_points(
    tasks: Iterable[Dict[str, Any]], routes: Iterable[Dict[str, Any]],
    points: Dict[str, Dict[str, Any]], blocked_pairs: Iterable[Tuple[str, str]],
    seed: int, avoid_pairs=None,
) -> List[Dict[str, Any]]:
    """Choose a reproducible vehicle spawn before each task service origin.

    The mission route remains ``from_point_id -> to_point_id``.  This helper
    only adds a preceding P5-backed deadhead leg.  If the map library has no
    admissible incoming route, the vehicle stays at the service origin and
    the fallback is explicit rather than pretending an unverified route is
    usable.
    """
    result = [dict(item) for item in tasks]
    incoming: Dict[str, List[Dict[str, Any]]] = {}
    for raw in routes:
        item = dict(raw)
        source = str(item.get("from_point_id"))
        target = str(item.get("to_point_id"))
        length = item.get("route_length_m")
        if (source not in points or target not in points or source == target
                or length is None or float(length) <= 0):
            continue
        incoming.setdefault(target, []).append(item)
    blocked = {
        tuple(sorted((str(a), str(b)))) for a, b in blocked_pairs
    }
    blocked.update(
        tuple(sorted((str(a), str(b)))) for a, b in (avoid_pairs or set())
    )
    occupied = {
        str(item["from_point_id"]) for item in result
    } | {
        str(item["to_point_id"]) for item in result
    }
    selected_spawns: Set[str] = set()
    randomizer = Random(int(seed) + 18181)
    for item in result:
        service_origin = str(item["from_point_id"])
        # A support-response truck is operationally staged at its selected
        # service origin.  Chaining an independently selected deadhead leg
        # into a directed support route can leave the truck facing the wrong
        # branch at the transfer point on the custom mine road network.  The
        # service origin/target pair remains seed-selected from admitted map
        # routes; only the response truck's initial standby position is bound
        # to that origin.  Haul and inspection vehicles retain seeded
        # deadhead starts.
        if (str(item.get("vehicle_role")) == "support"
                or str(item.get("task_type")) == "equipment_support"):
            spawn_point_id = service_origin
            selected_spawns.add(spawn_point_id)
            item.update({
                "spawn_point_id": spawn_point_id,
                "service_origin_point_id": service_origin,
                "service_target_point_id": str(item["to_point_id"]),
                "deadhead_route_length_m": 0.0,
                "deadhead_validation_status": (
                    "ROLE_ALIGNED_SERVICE_ORIGIN"
                ),
                "deadhead_route_source": (
                    "COMMON_SUPPORT_STAGING_POLICY_V1"
                ),
                "requires_deadhead": False,
                "staging_policy": "SUPPORT_READY_AT_SERVICE_ORIGIN",
            })
            continue
        candidates = [
            route for route in incoming.get(service_origin, [])
            if str(route["from_point_id"]) not in occupied
            and str(route["from_point_id"]) not in selected_spawns
            and not _blocked(
                str(route["from_point_id"]), selected_spawns, blocked
            )
        ]
        # Prefer a useful but bounded staging leg.  The exact length remains
        # recorded; this ordering is an engineering admission rule, not a
        # claim about measured dispatch-optimal parking locations.
        bounded = [
            route for route in candidates
            if 25.0 <= float(route["route_length_m"]) <= 1000.0
        ]
        pool = sorted(
            bounded or candidates,
            key=lambda route: (
                abs(float(route["route_length_m"]) - 250.0),
                str(route["from_point_id"]),
            ),
        )
        # Randomize only within the best bounded candidates.  This keeps
        # episodes seed-dependent without admitting arbitrary full-map
        # starts or discarding route evidence.
        shortlist = pool[:min(5, len(pool))]
        selected = randomizer.choice(shortlist) if shortlist else None
        if selected is None:
            spawn_point_id = service_origin
            deadhead_length = 0.0
            deadhead_status = "SERVICE_ORIGIN_FALLBACK_NO_ADMITTED_DEADHEAD"
            deadhead_source = "NO_P5_INCOMING_ROUTE_SELECTED"
        else:
            spawn_point_id = str(selected["from_point_id"])
            deadhead_length = float(selected["route_length_m"])
            deadhead_status = "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION"
            deadhead_source = str(
                selected.get("execution_evidence")
                or selected.get("validation_status")
                or "P5_PLANNER_REACHABLE"
            )
        selected_spawns.add(spawn_point_id)
        item.update({
            "spawn_point_id": spawn_point_id,
            "service_origin_point_id": service_origin,
            "service_target_point_id": str(item["to_point_id"]),
            "deadhead_route_length_m": round(deadhead_length, 3),
            "deadhead_validation_status": deadhead_status,
            "deadhead_route_source": deadhead_source,
            "requires_deadhead": spawn_point_id != service_origin,
        })
    return result


def _episode_tasks(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge route facts and lifecycle facts by task ID.

    Several baseline runners keep map facts in ``map_resource_task_draft``
    and task status in ``initial_task_states``.  Selecting only one list loses
    either the route or the assignment, so V2 performs an explicit join.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for name in ("map_resource_task_draft", "initial_task_states", "tasks"):
        values = result.get(name)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            task_id = str(item["task_id"])
            if task_id not in merged:
                merged[task_id] = {}
                order.append(task_id)
            merged[task_id].update(item)
    return [merged[task_id] for task_id in order]


def _episode_fleet(result: Dict[str, Any]) -> Dict[str, Any]:
    fleet = result.get("fleet")
    if isinstance(fleet, dict):
        snapshot = dict(fleet)
        vehicles = snapshot.get("vehicles")
        if isinstance(vehicles, list):
            semantics = {
                str(item.get("vehicle_id")): item
                for item in result.get("map_resource_task_draft", [])
                if isinstance(item, dict) and item.get("vehicle_id")
            }
            enriched = []
            for raw in vehicles:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                mission = semantics.get(str(item.get("vehicle_id")), {})
                item.update({
                    key: mission.get(key) for key in (
                        "initial_operational_state", "initial_payload_state",
                        "origin_area_id", "mission_type",
                    ) if mission.get(key) is not None
                })
                enriched.append(item)
            snapshot["vehicles"] = enriched
        return snapshot
    vehicles = result.get("initial_vehicle_states")
    return {
        "vehicles": [dict(item) for item in vehicles or [] if isinstance(item, dict)],
        "data_status": "PROJECTED_FROM_INITIAL_STATE",
    }


def _parameter_origin(name: str, scenario_key: str) -> Dict[str, str]:
    if name in {"spawn", "target", "road_closure_edge", "congestion_edge"}:
        return {
            "source": "MAP_RESOURCE_SEEDED_SELECTION",
            "calibration_status": "USES_RECORDED_MAP_FACTS_WITH_RUNTIME_VALIDATION_BOUNDARY",
        }
    if scenario_key == "s08" and name in {"weather", "risk", "event_time"}:
        return {
            "source": "PARAMETERIZED_SYNTHETIC_SLOPE_SCENARIO",
            "calibration_status": "NOT_REAL_MINE_OBSERVATION",
        }
    return {
        "source": "SEEDED_ENGINEERING_BOUNDED_SCENARIO_CONFIG",
        "calibration_status": "CALIBRATION_PENDING",
    }


def _v2_event_plan(
    scenario_key: str, expected_types: List[str],
    observed_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return one event shape even when a check-only run has not fired it."""
    by_type = {}
    for item in observed_events:
        if isinstance(item, dict):
            by_type[str(item.get("event_type"))] = dict(item)
    plan = []
    for sequence, event_type in enumerate(expected_types):
        observed = None
        for alias in EVENT_TYPE_ALIASES.get(str(event_type), (str(event_type),)):
            if alias in by_type:
                observed = by_type[alias]
                break
        semantics = EVENT_CONTRACTS.get(str(event_type), {
            "semantic_trigger": "configured scenario condition becomes true",
            "recovery_condition": "scenario-specific safe terminal condition is recorded",
        })
        tick = (observed or {}).get("trigger", {}).get("tick")
        recovery_tick = ((observed or {}).get("recovery_trigger") or {}).get("tick")
        plan.append({
            "sequence": sequence,
            "event_id": (observed or {}).get("event_id") or "{}-planned-{:02d}".format(
                scenario_key, sequence + 1
            ),
            "event_type": str(event_type),
            "status": (observed or {}).get("status") or "PLANNED_CONTRACT",
            "trigger": {
                "type": "state_condition",
                "condition": semantics["semantic_trigger"],
                "baseline_scheduled_tick": tick,
                "runtime_implementation": "BASELINE_TICK_ADAPTER",
            },
            "recovery": {
                "condition": semantics["recovery_condition"],
                "baseline_scheduled_tick": recovery_tick,
            },
            "affected_entities": dict((observed or {}).get("affected_entities") or {}),
            "native_event": observed,
        })
    return plan


def build_concrete_episode_v2(
    result: Dict[str, Any], scenario_key: str,
    scenario_spec: Dict[str, Any], scenario_events: List[Dict[str, Any]],
    map_context: Optional[Dict[str, Any]] = None,
) -> ConcreteEpisodeV2:
    """Project any S01-S09 baseline run into the common V2 contract.

    The function is simulator-independent and intentionally labels missing
    production resources as unavailable.  It enables simultaneous schema
    integration now, while the common production state machine is introduced
    incrementally in the next phase.
    """
    key = str(scenario_key).lower()
    tasks = _episode_tasks(result)
    fleet = _episode_fleet(result)
    vehicles = fleet.get("vehicles") if isinstance(fleet.get("vehicles"), list) else []
    expected_events = [str(item) for item in scenario_spec.get("events") or []]
    dimensions = [str(item) for item in scenario_spec.get("randomization") or []]
    explicit_resources = result.get("production_resources")
    explicit_resources = dict(explicit_resources) if isinstance(explicit_resources, dict) else {}
    loaders = list(explicit_resources.get("loaders") or [])
    dump_points = list(explicit_resources.get("dump_points") or [])
    route_admission = result.get("route_evidence_admission")
    if isinstance(route_admission, dict):
        route_admission = route_admission.get("status") or route_admission
    realized = {
        "seed": result.get("seed"),
        "vehicle_count": result.get("vehicle_count") or len(vehicles),
        "task_count": result.get("task_count") or len(tasks),
        "spawn_point_indices": list(result.get("spawn_point_indices") or []),
        "target_spawn_point_indices": list(result.get("target_spawn_point_indices") or []),
        "event_ticks": [
            item.get("trigger", {}).get("tick") for item in scenario_events
            if isinstance(item, dict)
        ],
    }
    criteria = [
        {"criterion_id": str(item), "status": "PENDING_RUNTIME_EVALUATION"}
        for item in scenario_spec.get("success_criteria") or []
    ]
    return ConcreteEpisodeV2(
        episode_id=str(result.get("run_id") or result.get("scenario_id") or key),
        scenario_key=key,
        scenario_id=str(result.get("scenario_id") or scenario_spec.get("scenario_id") or key),
        scenario_family=str(scenario_spec.get("scenario_type") or "generic"),
        scenario_version="2.0-contract",
        seed=result.get("seed"),
        complexity_profile={
            "vehicle_count": realized["vehicle_count"],
            "task_count": realized["task_count"],
            "event_type_count": len(expected_events),
            "randomized_dimension_count": len(dimensions),
            "level": (
                "compound" if len(expected_events) > 1 else
                "event" if expected_events else "baseline"
            ),
        },
        map_context=dict(map_context or result.get("map_context") or {}),
        fleet=fleet,
        production_system={
            "cycle_schema": "openpit.production-cycle.v1",
            "stages": list(PRODUCTION_CYCLE_STAGES),
            "loaders": loaders,
            "dump_points": dump_points,
            "resource_data_status": (
                "AVAILABLE_FROM_RUN_INPUT" if loaders or dump_points
                else "NOT_AVAILABLE_IN_BASELINE"
            ),
            "runtime_status": "CONTRACT_DEFINED_NOT_RUNTIME_IMPLEMENTED",
            "structural_parameters": dict(STRUCTURAL_PRODUCTION_PARAMETERS),
        },
        initial_state={
            "vehicles": [dict(item) for item in vehicles if isinstance(item, dict)],
            "tasks": tasks,
            "roads": dict(result.get("road_state") or {}),
            "environment": dict(result.get("environment") or {}),
        },
        randomization={
            "method": "CONSTRAINT_BOUNDED_SEEDED_SAMPLING",
            "declared_dimensions": dimensions,
            "realized_parameters": realized,
            "reproducible": result.get("seed") is not None,
            "parameter_provenance": {
                name: _parameter_origin(name, key) for name in dimensions
            },
        },
        event_plan=_v2_event_plan(key, expected_events, scenario_events),
        hard_constraints=[
            {"constraint_id": str(item), "type": "HARD", "status": "DECLARED"}
            for item in scenario_spec.get("constraints") or []
        ],
        admission={
            "contract_validation": "PASS",
            "carla_readiness": scenario_spec.get("carla_readiness"),
            "route_evidence": route_admission or "NOT_REPORTED",
            "runtime_generation_status": dict(result.get("episode_generation") or {}).get(
                "status", "PROJECTED_FROM_BASELINE_RESULT"
            ),
            "boundary": "V2 schema admission does not claim physical fleet safety",
        },
        acceptance={
            "criteria": criteria,
            "termination": scenario_spec.get("termination"),
            "baseline_outcome": {
                "status": result.get("status"),
                "completed_task_count": result.get("completed_task_count"),
                "task_count": result.get("task_count"),
                "closed_loop_status": result.get("closed_loop_status"),
            },
        },
        execution_binding={
            "current_adapter": scenario_spec.get("implementation_mode") or "unified_event_pipeline",
            "production_state_machine": "PENDING_COMMON_RUNTIME_IMPLEMENTATION",
            "carla_execution": "PRESERVED_BASELINE",
        },
        data_provenance={
            "scenario_definition": "SCENARIO_CATALOG",
            "spatial_facts": "MAP_RESOURCES_DB_WHEN_REPORTED",
            "runtime_facts": "OPENPIT_DB_AND_RUN_ARTIFACTS_WHEN_RECORDED",
            "synthetic_data_claim": "ENGINEERING_BOUNDED_SIMULATION_NOT_REAL_MINE_DATA",
        },
    )


def _native_event_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    native = event.get("native_event")
    if not isinstance(native, dict):
        return {}
    payload = native.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def simulate_structural_production_cycle(
    episode: Dict[str, Any], baseline_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute one deterministic, map-anchored production-cycle surrogate.

    Travel time uses recorded directed route length.  Service times and the
    reverse return leg are explicitly labelled engineering surrogates until
    calibrated loader/dump data and a verified reverse route are available.
    This function never claims CARLA physics or real mine productivity.
    """
    initial = episode.get("initial_state")
    tasks = initial.get("tasks") if isinstance(initial, dict) else []
    tasks = [dict(item) for item in tasks or [] if isinstance(item, dict)]
    if not tasks:
        return {
            "schema_version": PRODUCTION_RUNTIME_SCHEMA_VERSION,
            "status": "NOT_AVAILABLE_NO_TASKS",
            "tasks": [], "transitions": [], "event_applications": [],
            "boundary": "No production lifecycle was simulated.",
        }

    parameters = dict(STRUCTURAL_PRODUCTION_PARAMETERS)
    tick_seconds = float(parameters["tick_seconds"])
    empty_mps = float(parameters["empty_speed_kmh"]) / 3.6
    loaded_mps = float(parameters["loaded_speed_kmh"]) / 3.6
    loading_ticks = int(parameters["loading_service_ticks"])
    dumping_ticks = int(parameters["dumping_service_ticks"])
    handover_ticks = int(parameters["handover_service_ticks"])
    loader_free: Dict[str, int] = {}
    dump_free: Dict[str, int] = {}
    transitions: List[Dict[str, Any]] = []
    event_applications: List[Dict[str, Any]] = []
    runtime_tasks = []
    final_tasks = {
        str(item.get("task_id")): item
        for item in baseline_result.get("tasks", [])
        if isinstance(item, dict) and item.get("task_id")
    }

    for task_index, task in enumerate(tasks):
        task_id = str(task.get("task_id") or "task-{:02d}".format(task_index + 1))
        origin = task.get("from_point_id")
        target = task.get("to_point_id")
        route_length = task.get("route_length_m")
        assigned_vehicle = (
            task.get("assigned_vehicle_id") or task.get("vehicle_id")
        )
        task_transitions = []

        def change(tick: int, from_stage: Optional[str], to_stage: str,
                   reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
            record = {
                "task_id": task_id,
                "vehicle_id": assigned_vehicle,
                "tick": int(tick),
                "event_type": "task_production_stage_changed",
                "from_status": from_stage,
                "to_status": to_stage,
                "reason": reason,
            }
            if extra:
                record.update(extra)
            task_transitions.append(record)
            transitions.append(record)

        if origin is None or target is None or route_length is None:
            change(0, None, "terminal", "MAP_ROUTE_FACTS_NOT_AVAILABLE")
            runtime_tasks.append({
                "task_id": task_id, "vehicle_id": assigned_vehicle,
                "status": "NOT_RUN_MAP_ROUTE_FACTS_MISSING",
                "transitions": task_transitions,
            })
            continue

        loader_id = "loader@{}".format(origin)
        dump_id = "dump@{}".format(target)
        tick = 0
        change(tick, None, "idle", "production_task_admitted")
        change(tick, "idle", "to_loader", "vehicle_starts_at_map_anchored_origin")
        change(tick, "to_loader", "queue_loader", "loader_queue_entry")
        loading_start = max(tick, loader_free.get(loader_id, 0))
        if loading_start > tick:
            change(loading_start, "queue_loader", "loading", "loader_capacity_available", {
                "queue_ticks": loading_start - tick,
            })
        else:
            change(loading_start, "queue_loader", "loading", "loader_available")
        tick = loading_start + loading_ticks
        loader_free[loader_id] = tick
        change(tick, "loading", "loaded_haul", "loading_service_completed")

        travel_ticks = max(1, int(ceil(float(route_length) / loaded_mps / tick_seconds)))
        event_delay = 0
        transfer_count = 0
        for event in episode.get("event_plan", []):
            if not isinstance(event, dict):
                continue
            affected = event.get("affected_entities") or {}
            affected_tasks = set(str(item) for item in affected.get("task_ids", []))
            affected_vehicles = set(str(item) for item in affected.get("vehicle_ids", []))
            applies = (
                task_id in affected_tasks
                or (assigned_vehicle is not None and str(assigned_vehicle) in affected_vehicles)
            )
            if not applies:
                continue
            trigger_tick = event.get("trigger", {}).get("baseline_scheduled_tick")
            recovery_tick = event.get("recovery", {}).get("baseline_scheduled_tick")
            applied_tick = max(tick, int(trigger_tick or tick))
            event_type = str(event.get("event_type") or "scenario_event")
            payload = _native_event_payload(event)
            delay = 0
            action = "safe_hold_then_resume"
            if event_type == "vehicle_failure":
                delay = handover_ticks
                action = "release_and_reassign_task"
                transfer_count += 1
                replacement = final_tasks.get(task_id, {}).get("assigned_vehicle_id")
                change(applied_tick, "loaded_haul", "event_hold", "vehicle_failure_detected")
                if replacement and replacement != assigned_vehicle:
                    assigned_vehicle = replacement
                change(applied_tick + delay, "event_hold", "task_handover", action, {
                    "transfer_count": transfer_count,
                })
                change(applied_tick + delay, "task_handover", "loaded_haul", "legal_takeover_resumed")
            elif event_type == "extreme_weather":
                factor = float(payload.get("restricted_speed_factor") or 0.6)
                factor = min(1.0, max(0.1, factor))
                delay = int(ceil(travel_ticks / factor)) - travel_ticks
                action = "apply_weather_speed_policy"
                change(applied_tick, "loaded_haul", "event_hold", action)
                change(applied_tick + delay, "event_hold", "loaded_haul", "weather_capacity_restored")
            elif event_type == "congestion":
                delay = int(ceil(float(payload.get("initial_blockage_seconds") or 15.0)))
                action = "queue_for_shared_road_capacity"
                change(applied_tick, "loaded_haul", "event_hold", action)
                change(applied_tick + delay, "event_hold", "loaded_haul", "road_capacity_released")
            else:
                delay = max(1, int(recovery_tick or (applied_tick + 10)) - applied_tick)
                action = (
                    "replan_or_safe_hold" if event_type == "road_closure"
                    else "hold_until_safe_clearance"
                )
                change(applied_tick, "loaded_haul", "event_hold", action)
                change(applied_tick + delay, "event_hold", "loaded_haul", "event_recovery_condition_met")
            event_delay += delay
            event_applications.append({
                "event_id": event.get("event_id"), "event_type": event_type,
                "task_id": task_id, "scheduled_tick": trigger_tick,
                "applied_tick": applied_tick, "action": action,
                "delay_ticks": delay,
            })

        tick += travel_ticks + event_delay
        change(tick, "loaded_haul", "queue_dump", "directed_route_destination_reached_structurally")
        dumping_start = max(tick, dump_free.get(dump_id, 0))
        if dumping_start > tick:
            change(dumping_start, "queue_dump", "dumping", "dump_capacity_available", {
                "queue_ticks": dumping_start - tick,
            })
        else:
            change(dumping_start, "queue_dump", "dumping", "dump_point_available")
        tick = dumping_start + dumping_ticks
        dump_free[dump_id] = tick
        change(tick, "dumping", "returning", "dumping_service_completed")
        return_ticks = max(1, int(ceil(float(route_length) / empty_mps / tick_seconds)))
        tick += return_ticks
        change(tick, "returning", "terminal", "production_cycle_completed_structurally", {
            "return_route_evidence": "SURROGATE_REVERSE_ROUTE_NOT_VERIFIED",
        })
        runtime_tasks.append({
            "task_id": task_id, "vehicle_id": assigned_vehicle,
            "loader_id": loader_id, "dump_id": dump_id,
            "outbound_route_length_m": float(route_length),
            "outbound_route_evidence": task.get("execution_evidence") or task.get("validation_status"),
            "return_route_evidence": "SURROGATE_REVERSE_ROUTE_NOT_VERIFIED",
            "transfer_count": transfer_count, "completed_tick": tick,
            "status": "COMPLETED_STRUCTURAL_SURROGATE",
            "transitions": task_transitions,
        })

    completed = sum(
        item.get("status") == "COMPLETED_STRUCTURAL_SURROGATE"
        for item in runtime_tasks
    )
    return {
        "schema_version": PRODUCTION_RUNTIME_SCHEMA_VERSION,
        "status": "PASS" if completed == len(runtime_tasks) else "PARTIAL",
        "mode": "STRUCTURAL_PRODUCTION_SURROGATE",
        "task_count": len(runtime_tasks),
        "completed_task_count": completed,
        "tasks": runtime_tasks,
        "transitions": transitions,
        "event_applications": event_applications,
        "resources": {
            "loader_count": len(loader_free), "dump_point_count": len(dump_free),
            "resource_model": "MAP_ANCHORED_LOGICAL_SERVICE_RESOURCES",
        },
        "parameters": parameters,
        "boundary": (
            "Map route lengths are recorded facts when present; service times "
            "and reverse return travel are uncalibrated structural surrogates. "
            "No CARLA physics, collision, capacity or real productivity claim."
        ),
    }


def _integer_range(raw: Dict[str, Any], name: str,
                   fallback: Tuple[int, int]) -> Tuple[int, int]:
    value = raw.get(name, list(fallback))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise EpisodeGenerationError("{} must contain [minimum, maximum]".format(name))
    low, high = int(value[0]), int(value[1])
    if low < 0 or low > high:
        raise EpisodeGenerationError("{} is invalid".format(name))
    return low, high


def sample_event_timing(config: Any, scenario_key: str,
                        seed: int) -> Dict[str, int]:
    """Resolve one reproducible event schedule from the common config shape.

    Every scenario uses ``scenario_variables.randomization.events``.  V1
    intentionally supports tick windows rather than arbitrary probability
    code so the same scenario/seed is reproducible in structural and CARLA
    execution.
    """
    key = str(scenario_key).lower()
    randomization = config.scenario_variables.get("randomization", {})
    events = randomization.get("events", []) if isinstance(randomization, dict) else []
    event = events[0] if events and isinstance(events[0], dict) else {}
    raw = dict(event.get("parameters") or {})
    randomizer = Random(int(seed) + EVENT_TIMING_SALTS.get(key, 10101))

    if key == "s02":
        fixed = int(getattr(config.demo, "failure_tick", 20))
        low, high = _integer_range(
            raw if raw else event, "trigger_tick_range",
            tuple(event.get("tick_range", [fixed, fixed])),
        )
        return {"failure_tick": randomizer.randint(low, high)}

    defaults = {
        "s03": (30, 70), "s05": (30, 60), "s06": (30, 60),
        "s07": (30, 60),
    }
    if key in defaults:
        fixed_trigger, fixed_recovery = defaults[key]
        fixed_trigger = int(raw.get(
            "failure_tick" if key == "s03" else
            "event_tick" if key in {"s05", "s06"} else "trigger_tick",
            fixed_trigger,
        ))
        fixed_recovery = int(raw.get("recovery_tick", fixed_recovery))
        trigger_low, trigger_high = _integer_range(
            raw, "trigger_tick_range", (fixed_trigger, fixed_trigger)
        )
        duration_low, duration_high = _integer_range(
            raw, "duration_ticks_range",
            (max(1, fixed_recovery - fixed_trigger),
             max(1, fixed_recovery - fixed_trigger)),
        )
        trigger = randomizer.randint(trigger_low, trigger_high)
        recovery = trigger + randomizer.randint(duration_low, duration_high)
        trigger_name = (
            "failure_tick" if key == "s03" else
            "event_tick" if key in {"s05", "s06"} else "closure_tick"
        )
        return {trigger_name: trigger, "recovery_tick": recovery}

    if key == "s04":
        start_fixed = int(raw.get("blast_start_tick", 40))
        notice_fixed = int(raw.get("notice_tick", 20))
        clear_fixed = int(raw.get("clearance_tick", 60))
        start_range = _integer_range(raw, "trigger_tick_range", (start_fixed, start_fixed))
        notice_range = _integer_range(
            raw, "notice_lead_ticks_range",
            (max(1, start_fixed - notice_fixed), max(1, start_fixed - notice_fixed)),
        )
        duration_range = _integer_range(
            raw, "duration_ticks_range",
            (max(1, clear_fixed - start_fixed), max(1, clear_fixed - start_fixed)),
        )
        start = randomizer.randint(*start_range)
        notice = max(0, start - randomizer.randint(*notice_range))
        return {
            "notice_tick": notice, "blast_start_tick": start,
            "clearance_tick": start + randomizer.randint(*duration_range),
        }

    if key == "s09":
        road_fixed = int(raw.get("road_closure_tick", 30))
        failure_fixed = int(raw.get("vehicle_failure_tick", 40))
        recovery_fixed = int(raw.get("recovery_tick", 80))
        road_range = _integer_range(raw, "trigger_tick_range", (road_fixed, road_fixed))
        gap_range = _integer_range(
            raw, "inter_event_gap_ticks_range",
            (max(1, failure_fixed - road_fixed), max(1, failure_fixed - road_fixed)),
        )
        recovery_range = _integer_range(
            raw, "recovery_delay_ticks_range",
            (max(1, recovery_fixed - failure_fixed), max(1, recovery_fixed - failure_fixed)),
        )
        road = randomizer.randint(*road_range)
        failure = road + randomizer.randint(*gap_range)
        return {
            "road_closure_tick": road,
            "vehicle_failure_tick": failure,
            "recovery_tick": failure + randomizer.randint(*recovery_range),
        }
    return {}


def select_execution_route_facts(strict_p5_routes, all_p5_routes,
                                 physical_reached_pairs):
    """Join P5 metadata to an explicit P6-success subset without promotion.

    A non-strict P5 row is admitted only for an exact directed pair supplied
    by CARLA as ``PHYSICAL_REACHED``.  Its resulting status retains both
    facts, rather than relabelling it as ``PLANNER_REACHABLE``.
    """
    strict = {(str(item["from_point_id"]), str(item["to_point_id"])): dict(item)
              for item in strict_p5_routes}
    all_facts = {(str(item["from_point_id"]), str(item["to_point_id"])): dict(item)
                 for item in all_p5_routes}
    selected = []
    for pair in sorted((str(a), str(b)) for a, b in physical_reached_pairs):
        item = dict(strict.get(pair) or all_facts.get(pair) or {})
        if not item or item.get("route_length_m") is None:
            continue
        planner_status = str(item.get("validation_status") or "PLANNER_REACHABLE")
        item["planner_validation_status"] = planner_status
        item["execution_evidence"] = "P6_PHYSICAL_REACHED"
        item["validation_status"] = (
            "P6_PHYSICAL_REACHED" if planner_status == "PLANNER_REACHABLE"
            else "P6_PHYSICAL_REACHED_WITH_{}".format(planner_status)
        )
        selected.append(item)
    return selected


def _within_window(route: Dict[str, Any], minimum_length_m: float,
                   maximum_length_m: float) -> bool:
    length = route.get("route_length_m")
    return length is not None and minimum_length_m <= float(length) <= maximum_length_m


def _blocked(origin: str, selected_origins: Set[str], blocked_pairs: Set[Tuple[str, str]]) -> bool:
    return any(tuple(sorted((origin, item))) in blocked_pairs
               for item in selected_origins)


def build_s02_recoverable_task_drafts(routes: Iterable[Dict[str, Any]], blocked_pairs,
                                      seed: int, vehicle_count: int,
                                      minimum_length_m: float,
                                      maximum_length_m: float,
                                      avoid_pairs=None,
                                      operating_areas=None,
                                      scenario_key: str = "s02",
                                      ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Construct a failure scenario around an aligned takeover candidate.

    The failing truck A has A->T and a different inspection truck B has B->T.
    Giving both trucks independent work in the same service area is a useful
    mine-operation pattern: B can take over A's released work without first
    abandoning an unrelated destination.  It also keeps every CARLA leg
    inside the supplied execution-evidence set instead of inventing a later
    T->D recovery leg that has never been physically validated.
    """
    eligible = [dict(item) for item in routes
                if _within_window(item, minimum_length_m, maximum_length_m)]
    blocked = {tuple(sorted((str(a), str(b)))) for a, b in blocked_pairs}
    blocked.update(
        tuple(sorted((str(a), str(b))))
        for a, b in (avoid_pairs or set())
    )
    randomizer = Random(int(seed) + 2002)
    roles = ["inspection", "inspection"] + [
        item for item in fleet_roles(vehicle_count) if item != "inspection"
    ]
    if len(roles) != int(vehicle_count):
        raise EpisodeGenerationError("S02_ROLE_COMPOSITION_INVALID")
    indexed = _area_index(operating_areas or [])
    semantic = bool(indexed)
    primary_options = [
        item for item in eligible
        if not semantic or _route_matches_mission(item, "inspection", indexed)
    ]
    randomizer.shuffle(primary_options)

    for primary in primary_options:
        origin_a, target = str(primary["from_point_id"]), str(primary["to_point_id"])
        fallback_options = [item for item in eligible
                            if str(item["to_point_id"]) == target
                            and str(item["from_point_id"]) != origin_a
                            and not _blocked(str(item["from_point_id"]), {origin_a}, blocked)]
        randomizer.shuffle(fallback_options)
        for fallback in fallback_options:
            origin_b = str(fallback["from_point_id"])
            selected = [primary, fallback]
            origins, destinations = {origin_a, origin_b}, {target}
            if origins & destinations:
                continue
            for role in roles[2:]:
                pool = [
                    item for item in eligible
                    if not semantic or _route_matches_mission(item, role, indexed)
                ]
                randomizer.shuffle(pool)
                route = next((
                    item for item in pool
                    if str(item["from_point_id"]) not in origins
                    and str(item["to_point_id"]) not in destinations
                    and str(item["from_point_id"]) not in destinations
                    and str(item["to_point_id"]) not in origins
                    and not _blocked(
                        str(item["from_point_id"]), origins, blocked
                    )
                ), None)
                if route is None:
                    break
                origin = str(route["from_point_id"])
                destination = str(route["to_point_id"])
                selected.append(route)
                origins.add(origin)
                destinations.add(destination)
            if len(selected) != int(vehicle_count):
                continue
            drafts = []
            for index, route in enumerate(selected):
                role = roles[index]
                drafts.append({
                    "task_id": "{}-seed-{}-task-{:02d}".format(
                        str(scenario_key).lower(), seed, index + 1
                    ),
                    "vehicle_id": "{}_vehicle_{:02d}".format(role, index + 1),
                    "vehicle_role": role, "vehicle_slot": index + 1,
                    **route,
                })
            failed_index = randomizer.randrange(2)
            candidate_index = 1 - failed_index
            candidate_route = selected[candidate_index]
            return drafts, {
                "failed_vehicle_id": drafts[failed_index]["vehicle_id"],
                "failed_task_id": drafts[failed_index]["task_id"],
                "takeover_candidate_vehicle_ids": [
                    drafts[candidate_index]["vehicle_id"]
                ],
                "candidate_alignment": "SHARED_SERVICE_TARGET",
                "fallback_route": {
                    "from_point_id": candidate_route["from_point_id"],
                    "to_point_id": target,
                    "route_length_m": candidate_route.get("route_length_m"),
                    "planner_version": candidate_route.get("planner_version"),
                },
            }
    raise EpisodeGenerationError(
        "NO_TAKEOVER_CANDIDATE: no seeded S02 aligned primary/takeover route set satisfies hard map constraints"
    )


def _materialize_workload(config: Any, tasks: List[Dict[str, Any]], points: Dict[str, Dict[str, Any]],
                          planner_routes: List[Dict[str, Any]], effective_seed: int,
                          vehicle_count: int, minimum_length_m: float,
                          maximum_length_m: float, scenario_key: str,
                          metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    vehicles, zones, runtime_tasks = [], [], []
    for item in tasks:
        role = item["vehicle_role"]
        display_prefix, equipment_type, capabilities, requirements = ROLE_DETAILS[role]
        start = points[item.get("spawn_point_id") or item["from_point_id"]]
        target = points[item["to_point_id"]]
        preferred_vehicle_id = item.get("preferred_vehicle_id")
        if preferred_vehicle_id is None and scenario_key == "s02" and item["vehicle_slot"] == 1:
            preferred_vehicle_id = item["vehicle_id"]
        vehicles.append(VehicleConfig(
            vehicle_id=item["vehicle_id"], display_name="{}{:02d}".format(display_prefix, item["vehicle_slot"]),
            equipment_type=equipment_type, role_name=role, blueprint="vehicle.cat.cat",
            spawn_point_index=int(start["spawn_point_index"]),
            mock_position=Position(float(start["x"]), float(start["y"]), float(start["z"])),
            target_speed_kmh=25.0 if role == "haul" else 20.0,
            capabilities=list(capabilities),
        ))
        target_area_name = item.get("target_area_display_name")
        zones.append(ZoneConfig(
            zone_id=item["task_id"], display_name=(
                str(target_area_name)
                if target_area_name else "{}地图路线候选终点".format(display_prefix)
            ),
            priority=60 if role == "haul" else 50 if role == "inspection" else 45,
            required_capabilities=list(requirements),
            target_spawn_point_index=int(target["spawn_point_index"]),
            mock_position=Position(float(target["x"]), float(target["y"]), float(target["z"])),
            preferred_vehicle_id=preferred_vehicle_id,
        ))
        runtime_tasks.append(Task(
            task_id=item["task_id"], zone_id=item["task_id"],
            priority=60 if role == "haul" else 50 if role == "inspection" else 45,
            required_capabilities=list(requirements), preferred_vehicle_id=preferred_vehicle_id,
            task_type=str(item.get("task_type") or (
                "haul_transport" if role == "haul" else
                "slope_inspection" if role == "inspection" else
                "equipment_support"
            )),
        ))
    dynamic = replace(config, scenario_id="{}-{}v-seed-{}".format(
        str(scenario_key).lower(), vehicle_count, effective_seed), vehicles=vehicles, zones=zones,
        fleet=replace(config.fleet, total_vehicles=vehicle_count,
                      available_vehicles=vehicle_count, active_vehicles=vehicle_count,
                      traffic_vehicles=0, role_policy="fixed"))
    # ``vehicle_origins`` remains the service-origin mapping consumed by the
    # established structural incident logic.  Physical spawn is separate so
    # road-closure/risk decisions continue to reason about the active mission
    # instead of silently changing meaning to its preceding deadhead leg.
    vehicle_origins = {item["vehicle_id"]: item["from_point_id"] for item in tasks}
    vehicle_spawn_points = {
        item["vehicle_id"]: item.get("spawn_point_id") or item["from_point_id"]
        for item in tasks
    }
    task_service_origins = {
        item["task_id"]: item["from_point_id"] for item in tasks
    }
    zone_targets = {item["task_id"]: item["to_point_id"] for item in tasks}
    route_costs = {(item["from_point_id"], item["to_point_id"]): item["route_length_m"]
                   for item in planner_routes}
    route_validation_statuses = {
        (item["from_point_id"], item["to_point_id"]): item.get(
            "validation_status", "PLANNER_REACHABLE"
        ) for item in planner_routes
    }
    operational_states = {
        item["vehicle_id"]: {
            "vehicle_id": item["vehicle_id"],
            "role": item["vehicle_role"],
            "operational_state": item.get("initial_operational_state"),
            "payload_state": item.get("initial_payload_state"),
            "spawn_point_id": item.get("spawn_point_id") or item["from_point_id"],
            "service_origin_point_id": item["from_point_id"],
            "service_target_point_id": item["to_point_id"],
            "deadhead_route_length_m": item.get("deadhead_route_length_m"),
            "deadhead_validation_status": item.get("deadhead_validation_status"),
            "origin_area_id": item.get("origin_area_id"),
            "mission_type": item.get("mission_type"),
            "data_origin": item.get("scenario_data_origin"),
        }
        for item in tasks
    }
    return {
        "config": dynamic, "seed": effective_seed, "vehicle_count": vehicle_count,
        "vehicles": vehicles, "zones": zones, "tasks": runtime_tasks,
        "task_drafts": tasks, "vehicle_origins": vehicle_origins,
        "vehicle_spawn_points": vehicle_spawn_points,
        "task_service_origins": task_service_origins,
        "zone_targets": zone_targets, "route_costs": route_costs,
        "route_validation_statuses": route_validation_statuses,
        "vehicle_operational_states": operational_states,
        "minimum_length_m": minimum_length_m, "maximum_length_m": maximum_length_m,
        "generation": {"status": "ADMITTED", "scenario_key": scenario_key,
                       "route_evidence_mode": (
                           "P6_PHYSICAL_REACHED_JOINED_WITH_P5_METADATA"
                           if any(item.get("execution_evidence") == "P6_PHYSICAL_REACHED"
                                  for item in planner_routes)
                           else "P5_PLANNER_REACHABLE"
                       ), "metadata": dict(metadata or {})},
    }


def generate_map_constrained_workload(config: Any, seed: Optional[int] = None,
                                      vehicle_count: int = 6,
                                      minimum_length_m: float = 500.0,
                                      maximum_length_m: float = 3000.0,
                                      scenario_key: str = "s01",
                                      eligible_pairs: Optional[Set[Tuple[str, str]]] = None,
                                      deadhead_eligible_pairs: Optional[
                                          Set[Tuple[str, str]]
                                      ] = None) -> Dict[str, Any]:
    """Generate one reproducible, admitted map-backed workload.

    Without ``eligible_pairs`` the workload uses strict P5 planner facts.
    CARLA callers supply P6 ``PHYSICAL_REACHED`` pairs; the generator retains
    the original P5 status even where P6 admits that exact pair.
    """
    binding = config.map_resource
    if binding is None or not binding.database_path or not binding.map_id or not binding.resource_version:
        raise EpisodeGenerationError("MAP_RESOURCE_BINDING_REQUIRED")
    effective_seed = config.demo.random_seed if seed is None else int(seed)
    key = str(scenario_key).lower()
    with MapResourceStore(binding.database_path) as store:
        points = {item["point_id"]: item for item in store.verified_spawn_points(binding.map_id)}
        strict_p5_routes = list(store.planner_reachable_pairs(
            binding.map_id, binding.resource_version
        ))
        all_route_facts = list(store.planner_route_facts(
            binding.map_id, binding.resource_version
        ))
        if eligible_pairs is not None:
            planner_routes = select_execution_route_facts(
                strict_p5_routes,
                all_route_facts,
                eligible_pairs,
            )
        else:
            planner_routes = [dict(item, validation_status="PLANNER_REACHABLE",
                                   execution_evidence="P5_PLANNER_REACHABLE")
                              for item in strict_p5_routes]
        blocked_pairs = store.blocked_dual_spawn_pairs(binding.map_id, binding.resource_version)
        static_avoid_pairs = store.static_inferred_conflict_pairs(
            binding.map_id, binding.resource_version
        )
        operating_areas = list(store.operating_areas(
            binding.map_id, binding.resource_version
        ))
    if key in {"s02", "s09"}:
        try:
            tasks, metadata = build_s02_recoverable_task_drafts(
                planner_routes, blocked_pairs, effective_seed, vehicle_count,
                minimum_length_m, maximum_length_m,
                avoid_pairs=static_avoid_pairs,
                operating_areas=operating_areas,
                scenario_key=key,
            )
            semantic_status = "SEMANTIC_AREA_AND_RECOVERY_CONSTRAINED"
        except EpisodeGenerationError as exc:
            tasks, metadata = build_s02_recoverable_task_drafts(
                planner_routes, blocked_pairs, effective_seed, vehicle_count,
                minimum_length_m, maximum_length_m,
                avoid_pairs=static_avoid_pairs,
                scenario_key=key,
            )
            metadata["semantic_fallback_reason"] = str(exc)
            semantic_status = "RECOVERY_CONSTRAINED_ROUTE_WITH_SEMANTIC_FALLBACK"
    else:
        try:
            tasks = _semantic_seeded_tasks(
                planner_routes, blocked_pairs, effective_seed, vehicle_count,
                minimum_length_m, maximum_length_m, key, operating_areas,
                avoid_pairs=static_avoid_pairs,
            )
            semantic_status = "SEMANTIC_AREA_CONSTRAINED"
            metadata = {}
        except EpisodeGenerationError as exc:
            # Older resource databases may not yet contain the semantic area
            # profile.  Preserve executable Baseline behaviour, but make the
            # weaker endpoint meaning explicit in every episode and DB row.
            tasks = constrained_seeded_tasks(
                planner_routes, blocked_pairs, effective_seed, vehicle_count,
                minimum_length_m, maximum_length_m, scenario_key=key,
                avoid_spawn_pairs=static_avoid_pairs,
            )
            semantic_status = "MAP_ROUTE_FALLBACK_PENDING_AREA_COVERAGE"
            metadata = {"semantic_fallback_reason": str(exc)}
    tasks = _annotate_task_semantics(
        tasks, operating_areas, semantic_status
    )
    tasks = _assign_deadhead_staging_points(
        # Structural runs may use the full P5 set.  CARLA callers supply a
        # P6-filtered ``planner_routes`` set through ``eligible_pairs``;
        # deadhead travel must obey that same physical-admission boundary.
        tasks, (
            select_execution_route_facts(
                strict_p5_routes, all_route_facts,
                deadhead_eligible_pairs,
            )
            if deadhead_eligible_pairs is not None else planner_routes
        ), points, blocked_pairs, effective_seed,
        avoid_pairs=static_avoid_pairs,
    )
    if eligible_pairs is not None or deadhead_eligible_pairs is not None:
        # P6 proves one directed service route at a time.  It does not prove
        # that an independently selected incoming route can be chained into
        # that service route with a valid lane/heading transition.  CARLA
        # episodes therefore stage every vehicle at its seed-selected service
        # origin and execute only the admitted P6 pair.  Randomness remains in
        # the origin, destination, role allocation and events; structural-only
        # episodes may still exercise the separate deadhead abstraction.
        for item in tasks:
            service_origin = str(item["from_point_id"])
            item.update({
                "spawn_point_id": service_origin,
                "service_origin_point_id": service_origin,
                "service_target_point_id": str(item["to_point_id"]),
                "deadhead_route_length_m": 0.0,
                "deadhead_validation_status": (
                    "NOT_REQUIRED_ALREADY_AT_SERVICE_ORIGIN"
                ),
                "deadhead_route_source": (
                    "P6_SINGLE_LEG_PHYSICAL_ADMISSION"
                ),
                "requires_deadhead": False,
                "staging_policy": "P6_SERVICE_ORIGIN_STAGING",
            })
            item["preferred_vehicle_id"] = item["vehicle_id"]
            item["execution_vehicle_binding"] = (
                "P6_INITIAL_ROUTE_ADMISSION"
            )
    metadata = dict(metadata)
    metadata["mission_generation"] = {
        "selection_status": semantic_status,
        "selection_order": (
            "role_and_mission_then_operating_area_then_route"
            if semantic_status.startswith("SEMANTIC_AREA")
            else "route_constraints_then_explicit_semantic_annotation"
        ),
        "operating_area_count": len(operating_areas),
        "task_types": [item.get("task_type") for item in tasks],
        "mission_types": [item.get("mission_type") for item in tasks],
        "deadhead_route_count": sum(
            bool(item.get("requires_deadhead")) for item in tasks
        ),
        "deadhead_fallback_count": sum(
            not bool(item.get("requires_deadhead")) for item in tasks
        ),
        "boundary": (
            "Operating areas are parameterized engineering semantics. "
            "They are not measured real-mine loading, dumping or station data."
        ),
    }
    metadata["fleet_route_admission"] = {
        "selection": "seeded_random_with_hard_origin_destination_and_initial_spawn_constraints",
        "requested_vehicle_count": int(vehicle_count),
        "selected_route_count": len(tasks),
        "recorded_dual_spawn_block_count": len(blocked_pairs),
        "static_proximity_avoidance_count": len(static_avoid_pairs),
        "selected_origin_point_ids": [item["from_point_id"] for item in tasks],
        "selected_target_point_ids": [item["to_point_id"] for item in tasks],
        "boundary": (
            "P4 static proximity candidates are conservatively avoided during "
            "initial spawn selection; this is not multi-vehicle collision, "
            "clearance, traffic, or route-conflict validation."
        ),
    }
    return _materialize_workload(config, tasks, points, planner_routes,
                                 effective_seed, vehicle_count, minimum_length_m,
                                 maximum_length_m, key, metadata)
