"""Import CARLA ``Map.get_topology()`` as versioned directed road facts.

This captures CARLA's navigation topology, not a physical driving or
multi-vehicle safety result.  It is intentionally separate from P5/P6 route
validation facts.
"""
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Tuple


SOURCE_CARLA_TOPOLOGY = "CARLA_RUNTIME_TOPOLOGY_V1"
TOPOLOGY_IMPORTED = "CARLA_TOPOLOGY_IMPORTED"


def _waypoint_fact(waypoint: Any) -> Dict[str, object]:
    location = waypoint.transform.location
    return {
        "road_id": str(waypoint.road_id),
        "lane_id": int(waypoint.lane_id),
        "s": round(float(waypoint.s), 3),
        "x": round(float(location.x), 3),
        "y": round(float(location.y), 3),
        "z": round(float(location.z), 3),
    }


def _node_id(map_id: str, fact: Dict[str, object]) -> str:
    return "carla-topology:{}:{}:{}:{:.3f}:{:.3f}:{:.3f}".format(
        map_id, fact["road_id"], fact["lane_id"], fact["s"], fact["x"], fact["y"]
    )


def _edge_id(map_id: str, source_id: str, target_id: str) -> str:
    digest = hashlib.sha256((source_id + "->" + target_id).encode("utf-8")).hexdigest()[:20]
    return "carla-topology-edge:{}:{}".format(map_id, digest)


def topology_records(carla_map: Any, map_id: str) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """Convert CARLA topology pairs to deterministic node/edge records."""
    pairs = list(carla_map.get_topology())
    normalized = []
    for source, target in pairs:
        source_fact, target_fact = _waypoint_fact(source), _waypoint_fact(target)
        normalized.append((source_fact, target_fact))
    normalized.sort(key=lambda item: (_node_id(map_id, item[0]), _node_id(map_id, item[1])))

    nodes: Dict[str, Dict[str, object]] = {}
    edges: List[Dict[str, object]] = []
    for source_fact, target_fact in normalized:
        source_id, target_id = _node_id(map_id, source_fact), _node_id(map_id, target_fact)
        nodes[source_id] = dict(source_fact, node_id=source_id)
        nodes[target_id] = dict(target_fact, node_id=target_id)
        if source_id == target_id:
            continue
        length = math.sqrt(
            (target_fact["x"] - source_fact["x"]) ** 2
            + (target_fact["y"] - source_fact["y"]) ** 2
            + (target_fact["z"] - source_fact["z"]) ** 2
        )
        edges.append({
            "edge_id": _edge_id(map_id, source_id, target_id),
            "from_node_id": source_id,
            "to_node_id": target_id,
            "road_id": source_fact["road_id"],
            "lane_id": source_fact["lane_id"],
            "length_m": length,
            "geometry_json": json.dumps([
                [source_fact["x"], source_fact["y"], source_fact["z"]],
                [target_fact["x"], target_fact["y"], target_fact["z"]],
            ]),
        })
    return [nodes[key] for key in sorted(nodes)], edges


def import_carla_topology(
    store: Any, carla_map: Any, map_id: str, resource_version: str
) -> Dict[str, object]:
    """Replace only this importer source's topology rows, idempotently."""
    if not store.has_map_resource_version(map_id, resource_version):
        raise ValueError("map resource version does not exist: {}/{}".format(map_id, resource_version))
    nodes, edges = topology_records(carla_map, map_id)
    connection = store.connection
    # Edge-derived candidates are invalid whenever the source topology is
    # replaced.  Captured/manual route candidates use different sources and
    # remain untouched.
    connection.execute(
        "DELETE FROM route_candidates WHERE map_id=? AND resource_version=? "
        "AND source='CARLA_TOPOLOGY_DERIVED_ROUTE_V1'",
        (map_id, resource_version),
    )
    connection.execute(
        "DELETE FROM road_edges WHERE map_id=? AND resource_version=? AND source=?",
        (map_id, resource_version, SOURCE_CARLA_TOPOLOGY),
    )
    connection.execute(
        "DELETE FROM road_nodes WHERE map_id=? AND resource_version=? AND source=?",
        (map_id, resource_version, SOURCE_CARLA_TOPOLOGY),
    )
    connection.executemany(
        """
        INSERT INTO road_nodes(
            node_id,map_id,resource_version,point_id,road_id,lane_id,x,y,z,
            node_type,source,validation_status,notes
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (node["node_id"], map_id, resource_version, None, node["road_id"],
             node["lane_id"], node["x"], node["y"], node["z"],
             "CARLA_TOPOLOGY_ENDPOINT", SOURCE_CARLA_TOPOLOGY,
             TOPOLOGY_IMPORTED,
             "CARLA Map.get_topology() endpoint; physical traversal not verified")
            for node in nodes
        ],
    )
    connection.executemany(
        """
        INSERT INTO road_edges(
            edge_id,map_id,resource_version,from_node_id,to_node_id,road_id,lane_id,
            length_m,status,source,validation_status,geometry_json,notes
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (edge["edge_id"], map_id, resource_version, edge["from_node_id"],
             edge["to_node_id"], edge["road_id"], edge["lane_id"],
             edge["length_m"], "OPEN", SOURCE_CARLA_TOPOLOGY,
             TOPOLOGY_IMPORTED, edge["geometry_json"],
             "CARLA Map.get_topology() directed segment; no physical or fleet-safety validation")
            for edge in edges
        ],
    )
    connection.commit()
    return {
        "map_id": map_id,
        "resource_version": resource_version,
        "source": SOURCE_CARLA_TOPOLOGY,
        "topology_pair_count": len(list(carla_map.get_topology())),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "validation_status": TOPOLOGY_IMPORTED,
        "boundary": "CARLA navigation topology only; not physical driving, clearance, collision or fleet validation.",
    }
