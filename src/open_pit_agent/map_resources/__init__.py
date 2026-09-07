"""Persistent, map-scoped spatial resources for open-pit scenarios."""
from .store import MapResourceStore
from .xodr import SOURCE_XODR, parse_xodr, import_xodr, import_static_xodr, parse_open_drive
from .road_graph import (
    GraphEdge, RoadGraph, RoutePlan, RoutePlanner,
    ROUTE_PLAN_SCHEMA_VERSION, TOPOLOGY_PLANNER_VERSION,
    derive_topology_route_candidates,
    identify_affected_routes, route_plans_from_store,
)
from .carla_calibrator import calibrate_spawn_points
from .point_conflicts import infer_static_point_conflicts
from .dual_spawn_calibrator import select_static_candidates, verify_dual_spawn_pairs
from .reachability import calibrate_planner_reachability, directed_point_pairs, route_facts, selected_directed_point_pairs
from .physical_route_validation import drive_single_route
from .carla_topology import import_carla_topology, topology_records, SOURCE_CARLA_TOPOLOGY

__all__ = ["MapResourceStore", "SOURCE_XODR", "parse_xodr", "parse_open_drive", "import_xodr", "import_static_xodr", "GraphEdge", "RoadGraph", "RoutePlan", "RoutePlanner", "ROUTE_PLAN_SCHEMA_VERSION", "TOPOLOGY_PLANNER_VERSION", "derive_topology_route_candidates", "identify_affected_routes", "route_plans_from_store", "calibrate_spawn_points", "infer_static_point_conflicts", "select_static_candidates", "verify_dual_spawn_pairs", "calibrate_planner_reachability", "directed_point_pairs", "selected_directed_point_pairs", "route_facts", "drive_single_route", "import_carla_topology", "topology_records", "SOURCE_CARLA_TOPOLOGY"]
