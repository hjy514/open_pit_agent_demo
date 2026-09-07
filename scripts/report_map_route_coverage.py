#!/usr/bin/env python3
"""Report full-map P5 coverage and read-only seeded structural task drafts."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.route_coverage import route_coverage_report, seeded_task_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/database/map_resources.db")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--seed", type=int, default=202601)
    parser.add_argument("--vehicle-count", type=int, choices=(6, 8), default=6)
    args = parser.parse_args()
    with MapResourceStore(args.database) as store:
        report = route_coverage_report(store, args.map_id, args.resource_version)
        routes = store.planner_reachable_pairs(args.map_id, args.resource_version)
    report["seeded_structural_task_draft"] = {
        "seed": args.seed, "vehicle_count": args.vehicle_count,
        "tasks": seeded_task_candidates(routes, args.seed, args.vehicle_count),
        "execution_mode": "structural_mock_only",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
