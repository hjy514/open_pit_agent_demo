"""Offline directed road graph built from Map Resource Library records."""
import heapq
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    from_node: str
    to_node: str
    length_m: float
    status: str = "OPEN"
    road_id: Optional[str] = None
    lane_id: Optional[int] = None
    geometry: Tuple[Tuple[float, float, float], ...] = ()


class RoadGraph:
    def __init__(self, edges: Iterable[GraphEdge] = ()) -> None:
        self.edges: Dict[str, GraphEdge] = {edge.edge_id: edge for edge in edges}
        self.adjacency: Dict[str, List[GraphEdge]] = {}
        for edge in self.edges.values():
            self.adjacency.setdefault(edge.from_node, []).append(edge)
        for values in self.adjacency.values():
            values.sort(key=lambda edge: (edge.to_node, edge.edge_id))

    @classmethod
    def from_store(cls, store: object, map_id: str, resource_version: str) -> "RoadGraph":
        rows = store.connection.execute(
            "SELECT edge_id, from_node_id, to_node_id, COALESCE(length_m, 0), "
            "status, road_id, lane_id, geometry_json "
            "FROM road_edges WHERE map_id=? AND resource_version=?",
            (map_id, resource_version),
        ).fetchall()
        edges = []
        for row in rows:
            try:
                raw_geometry = json.loads(row[7]) if row[7] else []
                geometry = tuple(
                    (float(item[0]), float(item[1]), float(item[2]))
                    for item in raw_geometry
                    if isinstance(item, (list, tuple)) and len(item) >= 3
                )
            except (TypeError, ValueError):
                geometry = ()
            edges.append(GraphEdge(
                str(row[0]), str(row[1]), str(row[2]), float(row[3]),
                str(row[4] or "OPEN"), None if row[5] is None else str(row[5]),
                None if row[6] is None else int(row[6]), geometry,
            ))
        return cls(edges)

    @staticmethod
    def _point_segment_projection(point, start, end):
        vector = tuple(end[index] - start[index] for index in range(3))
        relative = tuple(point[index] - start[index] for index in range(3))
        squared = sum(value * value for value in vector)
        fraction = 0.0 if squared == 0 else max(
            0.0, min(1.0, sum(relative[index] * vector[index] for index in range(3)) / squared)
        )
        projected = tuple(start[index] + fraction * vector[index] for index in range(3))
        distance = math.sqrt(sum((point[index] - projected[index]) ** 2 for index in range(3)))
        return distance, fraction

    def bind_point(self, point: Dict[str, object]) -> Optional[Dict[str, object]]:
        """Bind a map point to a directed road segment, never merely its endpoint."""
        location = (float(point["x"]), float(point["y"]), float(point["z"]))
        road_id = None if point.get("road_id") is None else str(point["road_id"])
        lane_id = None if point.get("lane_id") is None else int(point["lane_id"])
        same_lane = [
            edge for edge in self.edges.values()
            if edge.road_id == road_id and edge.lane_id == lane_id
            and len(edge.geometry) >= 2
        ]
        if not same_lane:
            return None
        candidates = []
        for edge in same_lane:
            distance, fraction = self._point_segment_projection(
                location, edge.geometry[0], edge.geometry[-1]
            )
            candidates.append((distance, edge.edge_id, fraction, edge))
        distance, _, fraction, edge = min(candidates)
        return {
            "point_id": str(point["point_id"]),
            "edge_id": edge.edge_id,
            "from_node_id": edge.from_node,
            "to_node_id": edge.to_node,
            "road_id": edge.road_id,
            "lane_id": edge.lane_id,
            "fraction": fraction,
            "projection_distance_m": distance,
        }

    def route_between_anchors(self, start: Dict[str, object],
                              goal: Dict[str, object],
                              closed_edge_ids: Iterable[str] = ()) -> Dict[str, object]:
        """Build a topology route including partial source and target segments."""
        closed = {str(item) for item in closed_edge_ids}
        start_edge = self.edges[str(start["edge_id"])]
        goal_edge = self.edges[str(goal["edge_id"])]
        if start_edge.edge_id in closed or goal_edge.edge_id in closed:
            return {"reachable": False, "edge_ids": [], "distance_m": None}
        start_fraction = float(start["fraction"])
        goal_fraction = float(goal["fraction"])
        if start_edge.edge_id == goal_edge.edge_id and start_fraction <= goal_fraction:
            return {
                "reachable": True,
                "edge_ids": [start_edge.edge_id],
                "distance_m": (goal_fraction - start_fraction) * start_edge.length_m,
            }
        middle = self.shortest_path(
            start_edge.to_node, goal_edge.from_node, closed_edge_ids=closed
        )
        if not middle["reachable"]:
            return {"reachable": False, "edge_ids": [], "distance_m": None}
        edge_ids = [start_edge.edge_id] + list(middle["edge_ids"]) + [goal_edge.edge_id]
        compact = []
        for edge_id in edge_ids:
            if not compact or compact[-1] != edge_id:
                compact.append(edge_id)
        distance = (
            (1.0 - start_fraction) * start_edge.length_m
            + float(middle["distance_m"])
            + goal_fraction * goal_edge.length_m
        )
        return {"reachable": True, "edge_ids": compact, "distance_m": distance}

    def shortest_path(self, start_node: str, goal_node: str, closed_edge_ids: Iterable[str] = ()) -> Dict[str, object]:
        start_node, goal_node = str(start_node), str(goal_node)
        closed: Set[str] = set(str(item) for item in closed_edge_ids)
        queue: List[Tuple[float, str]] = [(0.0, start_node)]
        distance: Dict[str, float] = {start_node: 0.0}
        previous: Dict[str, GraphEdge] = {}
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != distance.get(node):
                continue
            if node == goal_node:
                break
            for edge in self.adjacency.get(node, []):
                if edge.edge_id in closed or edge.status.upper() == "CLOSED":
                    continue
                candidate = cost + max(0.0, edge.length_m)
                if candidate < distance.get(edge.to_node, float("inf")):
                    distance[edge.to_node] = candidate
                    previous[edge.to_node] = edge
                    heapq.heappush(queue, (candidate, edge.to_node))
        if goal_node not in distance:
            return {"reachable": False, "start_node": start_node, "goal_node": goal_node, "node_ids": [], "edge_ids": [], "distance_m": None}
        edge_ids: List[str] = []
        node_ids: List[str] = [goal_node]
        cursor = goal_node
        while cursor != start_node:
            edge = previous[cursor]
            edge_ids.append(edge.edge_id)
            cursor = edge.from_node
            node_ids.append(cursor)
        edge_ids.reverse(); node_ids.reverse()
        return {"reachable": True, "start_node": start_node, "goal_node": goal_node, "node_ids": node_ids, "edge_ids": edge_ids, "distance_m": distance[goal_node]}


ROUTE_PLAN_SCHEMA_VERSION = "openpit.route-plan.v1"
TOPOLOGY_PLANNER_VERSION = "RoadGraph-Dijkstra-Topology-V1"


@dataclass(frozen=True)
class RoutePlan:
    """Simulator-neutral route result consumed by scenarios and execution."""

    reachable: bool
    edge_ids: Tuple[str, ...]
    distance_m: Optional[float]
    start_point_id: Optional[str]
    goal_point_id: Optional[str]
    closed_edge_ids: Tuple[str, ...]
    risk_blocked_edge_ids: Tuple[str, ...] = ()
    planner_version: str = TOPOLOGY_PLANNER_VERSION
    source: str = "map_resources.road_graph"

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": ROUTE_PLAN_SCHEMA_VERSION,
            "reachable": self.reachable,
            "planning_status": "PLANNED" if self.reachable else "UNREACHABLE",
            "edge_ids": list(self.edge_ids),
            "distance_m": self.distance_m,
            "start_point_id": self.start_point_id,
            "goal_point_id": self.goal_point_id,
            "closed_edge_ids": list(self.closed_edge_ids),
            "risk_blocked_edge_ids": list(self.risk_blocked_edge_ids),
            "planner_version": self.planner_version,
            "source": self.source,
            "validation_boundary": (
                "topology planning only; no CARLA physical traversal, clearance, "
                "collision or fleet-safety validation"
            ),
        }


class RoutePlanner:
    """Stable V1 planning interface backed by the existing Dijkstra graph."""

    def __init__(self, graph: RoadGraph) -> None:
        self.graph = graph

    def plan(
        self, start: Dict[str, object], goal: Dict[str, object],
        closed_edge_ids: Iterable[str] = (),
        road_state: Optional[Dict[str, str]] = None,
        risk_state: Optional[Dict[str, object]] = None,
    ) -> RoutePlan:
        closed = {str(item) for item in closed_edge_ids}
        for edge_id, status in (road_state or {}).items():
            if str(status).upper() == "CLOSED":
                closed.add(str(edge_id))
        risk_blocked = {
            str(item) for item in (risk_state or {}).get(
                "prohibited_edge_ids", []
            )
        }
        closed.update(risk_blocked)
        raw = self.graph.route_between_anchors(
            start, goal, closed_edge_ids=closed
        )
        return RoutePlan(
            reachable=bool(raw.get("reachable")),
            edge_ids=tuple(str(item) for item in raw.get("edge_ids", [])),
            distance_m=(
                None if raw.get("distance_m") is None
                else float(raw["distance_m"])
            ),
            start_point_id=(
                str(start["point_id"]) if start.get("point_id") is not None
                else None
            ),
            goal_point_id=(
                str(goal["point_id"]) if goal.get("point_id") is not None
                else None
            ),
            closed_edge_ids=tuple(sorted(closed)),
            risk_blocked_edge_ids=tuple(sorted(risk_blocked)),
        )


def identify_affected_routes(route_plans: Dict[str, Iterable[str]], closed_edge_ids: Iterable[str]) -> List[str]:
    """Return route/task keys whose edge sequence intersects closed edges."""
    closed = set(str(item) for item in closed_edge_ids)
    return sorted(str(key) for key, edges in route_plans.items() if closed.intersection(str(edge) for edge in (edges or [])))


def verified_point_anchors_from_store(store: Any, graph: RoadGraph,
                                      map_id: str) -> Dict[str, Dict[str, object]]:
    """Bind all P3-verified spawn points to their actual topology segments."""
    rows = store.connection.execute(
        "SELECT point_id,x,y,z,road_id,lane_id FROM map_points "
        "WHERE map_id=? AND validation_status='VERIFIED_SPAWN'",
        (str(map_id),),
    ).fetchall()
    anchors = {}
    for row in rows:
        point_id = str(row[0])
        anchor = graph.bind_point({
            "point_id": point_id, "x": row[1], "y": row[2], "z": row[3],
            "road_id": row[4], "lane_id": row[5],
        })
        if anchor is not None:
            anchors[point_id] = anchor
    return anchors

def route_plans_from_store(
    store: Any,
    map_id: str,
    resource_version: str,
    endpoint_pairs: Dict[str, Tuple[str, str]],
) -> Dict[str, List[str]]:
    """Load the best available candidate route for each task from the map DB.

    ``road_lane_sequence_json`` accepts ``{"edge_ids": ["..."]}`` or a
    plain JSON list. Missing/malformed candidates are skipped. This reports
    static graph membership only; it does not imply CARLA or heavy-truck
    safety validation.
    """
    plans: Dict[str, List[str]] = {}
    query = (
        "SELECT road_lane_sequence_json FROM route_candidates "
        "WHERE map_id=? AND resource_version=? AND from_point_id=? "
        "AND to_point_id=? AND validation_status NOT IN "
        "('REJECTED','TOPOLOGY_LENGTH_MISMATCH') "
        "ORDER BY candidate_rank ASC LIMIT 1"
    )
    for task_id, pair in endpoint_pairs.items():
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        row = store.connection.execute(
            query, (str(map_id), str(resource_version), str(pair[0]), str(pair[1]))
        ).fetchone()
        if not row or not row[0]:
            continue
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            edge_ids = payload.get("edge_ids") or payload.get("road_edge_ids") or []
        elif isinstance(payload, list):
            edge_ids = payload
        else:
            edge_ids = []
        if isinstance(edge_ids, list) and edge_ids:
            plans[str(task_id)] = [str(edge_id) for edge_id in edge_ids]
    return plans


TOPOLOGY_ROUTE_SOURCE = "CARLA_TOPOLOGY_DERIVED_ROUTE_V1"
TOPOLOGY_ROUTE_STATUS = "TOPOLOGY_DERIVED_UNVERIFIED"
TOPOLOGY_ROUTE_MISMATCH = "TOPOLOGY_LENGTH_MISMATCH"


def derive_topology_route_candidates(store: Any, map_id: str,
                                     resource_version: str,
                                     maximum_anchor_distance_m: float = 15.0
                                     ) -> Dict[str, object]:
    """Derive edge sequences for strict P5 pairs from persisted topology facts."""
    graph = RoadGraph.from_store(store, map_id, resource_version)
    point_rows = store.connection.execute(
        "SELECT point_id,x,y,z,road_id,lane_id FROM map_points "
        "WHERE map_id=? AND validation_status='VERIFIED_SPAWN' ORDER BY point_id",
        (map_id,),
    ).fetchall()
    points = {
        str(row[0]): {
            "point_id": str(row[0]), "x": row[1], "y": row[2], "z": row[3],
            "road_id": row[4], "lane_id": row[5],
        }
        for row in point_rows
    }
    anchors = {point_id: graph.bind_point(point) for point_id, point in points.items()}
    accepted_anchors = {
        point_id: anchor for point_id, anchor in anchors.items()
        if anchor is not None
        and float(anchor["projection_distance_m"]) <= float(maximum_anchor_distance_m)
    }
    records = []
    unreachable = 0
    seen_pairs = set()
    for pair in store.planner_reachable_pairs(map_id, resource_version):
        pair_key = (pair["from_point_id"], pair["to_point_id"])
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        start = accepted_anchors.get(pair["from_point_id"])
        goal = accepted_anchors.get(pair["to_point_id"])
        if start is None or goal is None:
            continue
        route = graph.route_between_anchors(start, goal)
        if not route["reachable"] or not route["edge_ids"]:
            unreachable += 1
            continue
        identity = "{}:{}:{}".format(
            resource_version, pair["from_point_id"], pair["to_point_id"]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        p5_length = pair.get("route_length_m")
        length_ratio = (
            None if p5_length is None or float(p5_length) <= 0
            else float(route["distance_m"]) / float(p5_length)
        )
        consistent = length_ratio is not None and 0.5 <= length_ratio <= 2.0
        records.append({
            "route_candidate_id": "topology-route:{}:{}".format(map_id, digest),
            "map_id": map_id,
            "resource_version": resource_version,
            "from_point_id": pair["from_point_id"],
            "to_point_id": pair["to_point_id"],
            "candidate_rank": 1,
            "planner_version": "RoadGraph-Dijkstra-Topology-V1",
            "route_hash": digest,
            "route_length_m": route["distance_m"],
            "junction_count": pair.get("junction_count"),
            "road_lane_sequence": {
                "edge_ids": route["edge_ids"],
                "start_anchor": start,
                "goal_anchor": goal,
                "p5_route_length_m": p5_length,
                "topology_to_p5_length_ratio": length_ratio,
            },
            "validation_status": (
                TOPOLOGY_ROUTE_STATUS if consistent else TOPOLOGY_ROUTE_MISMATCH
            ),
            "source": TOPOLOGY_ROUTE_SOURCE,
            "notes": (
                "Derived from persisted CARLA topology and strict P5 endpoint "
                "reachability; not physical heavy-truck or fleet-safety validation."
            ),
        })
    store.replace_route_candidates(
        records, map_id, resource_version, TOPOLOGY_ROUTE_SOURCE
    )
    distances = [
        float(anchor["projection_distance_m"])
        for anchor in anchors.values() if anchor is not None
    ]
    return {
        "map_id": map_id,
        "resource_version": resource_version,
        "source": TOPOLOGY_ROUTE_SOURCE,
        "validation_status": TOPOLOGY_ROUTE_STATUS,
        "verified_point_count": len(points),
        "anchored_point_count": len(accepted_anchors),
        "rejected_anchor_count": len(points) - len(accepted_anchors),
        "maximum_observed_projection_distance_m": max(distances) if distances else None,
        "maximum_allowed_projection_distance_m": float(maximum_anchor_distance_m),
        "route_candidate_count": len(records),
        "usable_route_candidate_count": sum(
            item["validation_status"] == TOPOLOGY_ROUTE_STATUS for item in records
        ),
        "length_mismatch_count": sum(
            item["validation_status"] == TOPOLOGY_ROUTE_MISMATCH for item in records
        ),
        "topology_unreachable_pair_count": unreachable,
        "boundary": (
            "Topology-derived edge membership only; no physical heavy-truck, "
            "clearance, collision or multi-vehicle safety validation."
        ),
    }
