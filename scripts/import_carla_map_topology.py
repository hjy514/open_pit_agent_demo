#!/usr/bin/env python3
"""Import the current CARLA map's directed navigation topology into the map DB."""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config
from open_pit_agent.map_resources import (
    MapResourceStore,
    derive_topology_route_candidates,
    import_carla_topology,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "mine_competition_demo.json")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--load-map", action="store_true", help="Ask CARLA to load the configured map before import")
    parser.add_argument(
        "--build-route-candidates", action="store_true",
        help="After topology import, derive edge sequences for strict P5 pairs",
    )
    parser.add_argument(
        "--route-candidates-only", action="store_true",
        help="Use topology already stored in SQLite; do not connect to CARLA",
    )
    parser.add_argument(
        "--maximum-anchor-distance-m", type=float, default=15.0,
        help="Reject a point whose projection is farther than this from its road edge",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.route_candidates_only:
        with MapResourceStore(args.database) as store:
            result = derive_topology_route_candidates(
                store, config.carla.map_name, args.resource_version,
                maximum_anchor_distance_m=args.maximum_anchor_distance_m,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    adapter = CarlaAdapter(config, load_map=args.load_map)
    adapter.connect()
    try:
        with MapResourceStore(args.database) as store:
            result = import_carla_topology(
                store, adapter.world.get_map(), config.carla.map_name,
                args.resource_version,
            )
            if args.build_route_candidates:
                result["route_candidates"] = derive_topology_route_candidates(
                    store, config.carla.map_name, args.resource_version,
                    maximum_anchor_distance_m=args.maximum_anchor_distance_m,
                )
        result["server_version"] = adapter.client.get_server_version()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
