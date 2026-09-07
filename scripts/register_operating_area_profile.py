#!/usr/bin/env python3
"""Validate or register a declarative operating-area profile in map_resources.db."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.scenario.operating_areas import (
    area_records, load_operating_area_profile, register_operating_area_profile,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=PROJECT_ROOT / "configs" / "map_resource_operating_areas_v1.json")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    profile = load_operating_area_profile(args.profile)
    records = list(area_records(profile))
    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN", "profile_id": profile["profile_id"], "areas": records}, ensure_ascii=False, indent=2))
        return 0
    with MapResourceStore(args.database) as store:
        count = register_operating_area_profile(store, profile)
        stored = list(store.operating_areas(profile["map_id"], profile["resource_version"]))
    print(json.dumps({"status": "PASS", "profile_id": profile["profile_id"], "registered_area_count": count, "areas": stored}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
