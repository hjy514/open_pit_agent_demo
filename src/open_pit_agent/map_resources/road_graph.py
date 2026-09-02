"""Offline directed road graph built from Map Resource Library records."""
import heapq
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    from_node: str
    to_node: str
    length_m: float
    status: str = "OPEN"


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
            "SELECT edge_id, from_node_id, to_node_id, COALESCE(length_m, 0), status "
            "FROM road_edges WHERE map_id=? AND resource_version=?",
            (map_id, resource_version),
        ).fetchall()
        return cls(GraphEdge(str(row[0]), str(row[1]), str(row[2]), float(row[3]), str(row[4] or "OPEN")) for row in rows)

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


def identify_affected_routes(route_plans: Dict[str, Iterable[str]], closed_edge_ids: Iterable[str]) -> List[str]:
    """Return route/task keys whose edge sequence intersects closed edges."""
    closed = set(str(item) for item in closed_edge_ids)
    return sorted(str(key) for key, edges in route_plans.items() if closed.intersection(str(edge) for edge in (edges or [])))

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
        "AND to_point_id=? AND validation_status <> 'REJECTED' "
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