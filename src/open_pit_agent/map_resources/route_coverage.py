"""Read-only P5 coverage reporting and seeded structural task sampling."""
from collections import Counter, defaultdict
from random import Random
from typing import Dict, Iterable, List


BOUNDARY = (
    "Candidates use strict P5 planner reachability only; they are not physical "
    "truck, clearance, collision, or multi-vehicle safety validation."
)


def route_coverage_report(store, map_id: str, resource_version: str) -> Dict[str, object]:
    """Summarise persisted P5 facts without changing the resource database."""
    routes = list(store.planner_reachable_pairs(map_id, resource_version))
    planner_version = routes[0]["planner_version"] if routes else "GlobalRoutePlanner"
    facts = store.reachable_pair_summary(map_id, resource_version, planner_version)
    outgoing, incoming = Counter(), Counter()
    for route in routes:
        outgoing[route["from_point_id"]] += 1
        incoming[route["to_point_id"]] += 1
    points = sorted(set(outgoing) | set(incoming))
    return {
        "map_id": map_id, "resource_version": resource_version,
        "planner_version": planner_version,
        "p5_status_counts": facts, "p5_total_pair_count": sum(facts.values()),
        "automatic_candidate_status": "PLANNER_REACHABLE",
        "automatic_candidate_route_count": len(routes),
        "point_coverage": [{"point_id": point, "outgoing_candidate_count": outgoing[point],
                            "incoming_candidate_count": incoming[point]}
                           for point in points],
        "boundary": BOUNDARY,
    }


def seeded_task_candidates(routes: Iterable[Dict[str, object]], seed: int,
                           vehicle_count: int) -> List[Dict[str, object]]:
    """Choose reproducible, distinct-origin structural task candidates.

    Distinct origins make the output useful as a future vehicle/task draft;
    this function intentionally does not claim simultaneous CARLA feasibility.
    """
    requested = int(vehicle_count)
    if requested < 1:
        raise ValueError("vehicle_count must be positive")
    by_origin = defaultdict(list)
    for route in routes:
        by_origin[str(route["from_point_id"])].append(dict(route))
    origins = sorted(by_origin)
    randomizer = Random(int(seed))
    randomizer.shuffle(origins)
    selected = []
    destinations = set()
    for origin in origins:
        options = list(by_origin[origin])
        randomizer.shuffle(options)
        route = next((item for item in options if item["to_point_id"] not in destinations), None)
        if route is None:
            continue
        destinations.add(route["to_point_id"])
        selected.append({"task_id": "seed-{}-task-{:02d}".format(seed, len(selected) + 1),
                         "vehicle_slot": len(selected) + 1, **route})
        if len(selected) == requested:
            return selected
    raise ValueError("insufficient distinct-origin/destination candidates: requested={}".format(requested))


def fleet_roles(vehicle_count: int) -> List[str]:
    """Return fixed role proportions for a structural mine fleet draft."""
    if int(vehicle_count) == 6:
        return ["haul", "haul", "haul", "inspection", "inspection", "support"]
    if int(vehicle_count) == 8:
        return ["haul", "haul", "haul", "haul", "inspection", "inspection", "support", "support"]
    raise ValueError("supported structural fleet sizes are 6 or 8")


def constrained_seeded_tasks(routes: Iterable[Dict[str, object]], blocked_spawn_pairs,
                             seed: int, vehicle_count: int, minimum_length_m: float,
                             maximum_length_m: float, minimum_incoming: int = 1,
                             minimum_outgoing: int = 1,
                             scenario_key: str = "s01",
                             avoid_spawn_pairs=None) -> List[Dict[str, object]]:
    """Build an auditable structural scenario draft from the global P5 index.

    Each draft uses strict planner routes within the configured length window,
    distinct origins/destinations, basic global in/out coverage, and excludes
    only *recorded* blocked simultaneous-spawn pairs.  It is not CARLA fleet
    authorisation.
    """
    if float(minimum_length_m) < 0 or float(maximum_length_m) < float(minimum_length_m):
        raise ValueError("invalid route length window")
    all_routes = [dict(item) for item in routes]
    outgoing, incoming = Counter(), Counter()
    for route in all_routes:
        outgoing[route["from_point_id"]] += 1
        incoming[route["to_point_id"]] += 1
    candidates = [route for route in all_routes
                  if float(minimum_length_m) <= float(route["route_length_m"] or 0) <= float(maximum_length_m)
                  and outgoing[route["from_point_id"]] >= int(minimum_outgoing)
                  and incoming[route["to_point_id"]] >= int(minimum_incoming)]
    by_origin = defaultdict(list)
    for route in candidates:
        by_origin[route["from_point_id"]].append(route)
    randomizer = Random(int(seed))
    origins = sorted(by_origin)
    randomizer.shuffle(origins)
    # ``blocked_spawn_pairs`` are observed failed simultaneous spawns.  The
    # optional set contains P4 static proximity inferences.  Treating the
    # latter as a conservative sampler exclusion reduces risky initial
    # layouts without promoting it to a collision/clearance conclusion.
    blocked = {tuple(sorted((str(a), str(b)))) for a, b in blocked_spawn_pairs}
    blocked.update(
        tuple(sorted((str(a), str(b))))
        for a, b in (avoid_spawn_pairs or set())
    )
    # Route-combination selection handles one-way map structure.  Selecting
    # all origins first can otherwise leave an origin with no legal endpoint.
    selected = None
    for _attempt in range(32):
        pool = list(candidates)
        randomizer.shuffle(pool)
        trial, trial_origins, trial_destinations = [], set(), set()
        for route in pool:
            origin, destination = route["from_point_id"], route["to_point_id"]
            if origin in trial_origins or destination in trial_destinations:
                continue
            if origin in trial_destinations or destination in trial_origins:
                continue
            if any(tuple(sorted((origin, chosen))) in blocked for chosen in trial_origins):
                continue
            trial.append(route)
            trial_origins.add(origin)
            trial_destinations.add(destination)
            if len(trial) == int(vehicle_count):
                selected = trial
                break
        if selected is not None:
            break
    if selected is None:
        raise ValueError("insufficient non-overlapping constrained task candidates: requested={}".format(vehicle_count))
    roles = fleet_roles(vehicle_count)
    return [{"task_id": "{}-seed-{}-task-{:02d}".format(
                 str(scenario_key).lower(), seed, index + 1),
             "vehicle_id": "{}_vehicle_{:02d}".format(roles[index], index + 1),
             "vehicle_role": roles[index], "vehicle_slot": index + 1, **route}
            for index, route in enumerate(selected)]
