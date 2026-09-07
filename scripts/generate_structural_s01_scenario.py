#!/usr/bin/env python3
"""Generate a seeded, constrained S01 structural scenario draft from P5 facts."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.route_coverage import BOUNDARY, constrained_seeded_tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/database/map_resources.db")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--seed", type=int, default=202601)
    parser.add_argument("--vehicle-count", type=int, choices=(6, 8), default=6)
    parser.add_argument("--min-route-length-m", type=float, default=500.0)
    parser.add_argument("--max-route-length-m", type=float, default=3000.0)
    parser.add_argument("--minimum-incoming", type=int, default=1)
    parser.add_argument("--minimum-outgoing", type=int, default=1)
    args = parser.parse_args()
    with MapResourceStore(args.database) as store:
        routes = list(store.planner_reachable_pairs(args.map_id, args.resource_version))
        blocked = store.blocked_dual_spawn_pairs(args.map_id, args.resource_version)
    tasks = constrained_seeded_tasks(routes, blocked, args.seed, args.vehicle_count,
                                     args.min_route_length_m, args.max_route_length_m,
                                     args.minimum_incoming, args.minimum_outgoing)
    print(json.dumps({"scenario_id": "s01-normal-{}v-seed-{}".format(args.vehicle_count, args.seed),
                      "execution_mode": "structural_mock_only", "seed": args.seed,
                      "vehicle_count": args.vehicle_count,
                      "route_constraints": {"minimum_length_m": args.min_route_length_m,
                                            "maximum_length_m": args.max_route_length_m,
                                            "minimum_incoming": args.minimum_incoming,
                                            "minimum_outgoing": args.minimum_outgoing,
                                            "candidate_status": "PLANNER_REACHABLE"},
                      "tasks": tasks, "boundary": BOUNDARY}, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
