"""Dependency-free OpenDRIVE importer for Map Resource Library Phase 2."""
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SOURCE_XODR = "SOURCE_XODR"
UNVERIFIED = "UNVERIFIED"

def _float(element: Optional[ET.Element], name: str, default: float = 0.0) -> float:
    if element is None: return default
    try: return float(element.get(name, default))
    except (TypeError, ValueError): return default

def _sample_geometry(geometry: ET.Element, spacing: float) -> List[Tuple[float, float, float]]:
    s0, x0, y0 = (_float(geometry, n) for n in ("s", "x", "y"))
    heading, length = _float(geometry, "hdg"), max(0.0, _float(geometry, "length"))
    count = max(1, int(math.ceil(length / max(spacing, 0.01)))); step = length / count
    child = next(iter(geometry), None); curvature = _float(child, "curvature") if child is not None and child.tag.endswith("arc") else 0.0
    points = []
    for i in range(count + 1):
        d = min(length, i * step)
        if abs(curvature) < 1e-12:
            x, y = x0 + d * math.cos(heading), y0 + d * math.sin(heading)
        else:
            radius = 1.0 / curvature
            x = x0 + radius * (math.sin(heading + curvature * d) - math.sin(heading))
            y = y0 - radius * (math.cos(heading + curvature * d) - math.cos(heading))
        points.append((s0 + d, x, y))
    return points

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _children(element: ET.Element, name: str):
    return [child for child in element.iter() if _local(child.tag) == name]

def parse_xodr(path: Path, spacing_m: float = 10.0) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve(); root = ET.parse(str(path)).getroot(); roads = []
    for road in _children(root, "road"):
        road_id, plan = str(road.get("id", "")), next((child for child in road if _local(child.tag) == "planView"), None); samples = []
        if plan is not None:
            for geometry in _children(plan, "geometry"): samples.extend(_sample_geometry(geometry, spacing_m))
        deduped = []
        for item in sorted(samples, key=lambda value: value[0]):
            if not deduped or abs(item[0] - deduped[-1][0]) > 1e-8: deduped.append(item)
        lanes = []
        for section_index, section in enumerate(_children(road, "laneSection")):
            for side in ("left", "center", "right"):
                side_element = next((child for child in section if _local(child.tag) == side), None)
                if side_element is None: continue
                for lane in [child for child in side_element if _local(child.tag) == "lane"]:
                    width = next((child for child in lane if _local(child.tag) == "width"), None)
                    lanes.append({"section_index": section_index, "id": int(lane.get("id", 0)), "type": lane.get("type", "unknown"), "level": lane.get("level", "false").lower() == "true", "width_m": _float(width, "a", 0.0) if width is not None else None, "side": side})
        roads.append({"id": road_id, "name": road.get("name"), "length_m": _float(road, "length"), "junction": road.get("junction"), "samples": [[round(s, 6), round(x, 6), round(y, 6)] for s, x, y in deduped], "lanes": lanes})
    junctions = []
    junction_root = next((child for child in root if _local(child.tag) == "junctions"), None)
    if junction_root is not None:
        for junction in [child for child in junction_root if _local(child.tag) == "junction"]:
            connections = []
            for connection in [child for child in junction if _local(child.tag) == "connection"]:
                links = [{"from": int(link.get("from", 0)), "to": int(link.get("to", 0))} for link in [child for child in connection if _local(child.tag) == "laneLink"]]
                connections.append({"id": connection.get("id"), "incoming_road": connection.get("incomingRoad"), "connecting_road": connection.get("connectingRoad"), "contact_point": connection.get("contactPoint"), "lane_links": links})
            junctions.append({"id": str(junction.get("id", "")), "name": junction.get("name"), "connections": connections})
    return {"header": {"rev_major": root.get("revMajor"), "rev_minor": root.get("revMinor"), "name": root.get("name")}, "roads": roads, "junctions": junctions, "xodr_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "source": SOURCE_XODR}

def import_xodr(store: Any, map_id: str, resource_version: str, xodr_path: Path, spacing_m: float = 10.0, calibration_run_id: Optional[str] = None) -> Dict[str, Any]:
    payload, conn = parse_xodr(xodr_path, spacing_m), store.connection
    conn.execute("DELETE FROM junction_connections WHERE resource_version = ?", (resource_version,)); conn.execute("DELETE FROM junctions WHERE map_id = ? AND resource_version = ?", (map_id, resource_version))
    for table in ("road_edges", "road_nodes"): conn.execute("DELETE FROM {} WHERE map_id = ? AND resource_version = ? AND source = ?".format(table), (map_id, resource_version, SOURCE_XODR))
    conn.execute("DELETE FROM map_points WHERE map_id = ? AND source = ?", (map_id, SOURCE_XODR))
    conn.execute("DELETE FROM road_clusters WHERE map_id = ? AND resource_version = ? AND source = ?", (map_id, resource_version, SOURCE_XODR))
    node_count = edge_count = point_count = 0
    for road in payload["roads"]:
        road_id, cluster_id = road["id"], "{}:{}:road".format(map_id, road["id"]); conn.execute("INSERT INTO road_clusters(cluster_id,map_id,resource_version,cluster_type,node_count,edge_count,validation_status,source,notes) VALUES(?,?,?,?,?,?,?,?,?)", (cluster_id, map_id, resource_version, "ROAD", 0, 0, UNVERIFIED, SOURCE_XODR, "Static OpenDRIVE geometry; CARLA validation pending"))
        samples, lane_ids = road["samples"], sorted(set(lane["id"] for lane in road["lanes"])) or [None]; lane_types = {lane["id"]: lane.get("type") for lane in road["lanes"]}; previous_by_lane = {}; road_nodes = road_edges = 0
        for index, sample in enumerate(samples):
            s, x, y = sample
            for lane_id in lane_ids:
                lane_token, node_id = "center" if lane_id is None else str(lane_id), "{}:{}:{}:{}".format(map_id, road_id, "center" if lane_id is None else lane_id, index)
                conn.execute("INSERT INTO road_nodes(node_id,map_id,resource_version,point_id,road_id,lane_id,x,y,z,node_type,source,validation_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (node_id, map_id, resource_version, None, road_id, lane_id, x, y, 0.0, "ROAD_WAYPOINT", SOURCE_XODR, UNVERIFIED, "OpenDRIVE reference geometry")); node_count += 1; road_nodes += 1
                point_id = "xodr:{}:{}:{}:{}".format(map_id, road_id, lane_token, index); conn.execute("INSERT INTO map_points(point_id,map_id,carla_spawn_point_index,x,y,z,yaw,road_id,lane_id,s,lane_type,travel_direction,road_cluster_id,heavy_truck_allowed,validation_status,source,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (point_id, map_id, None, x, y, 0.0, None, road_id, lane_id, s, lane_types.get(lane_id), None, cluster_id, None, UNVERIFIED, SOURCE_XODR, "Not a CARLA spawn point; geometry only")); conn.execute("UPDATE road_nodes SET point_id=? WHERE node_id=?", (point_id, node_id)); point_count += 1
                if lane_id in previous_by_lane:
                    previous, prev = samples[index - 1], previous_by_lane[lane_id]; edge_id = "{}:{}:{}:{}".format(map_id, road_id, lane_token, index); length = math.hypot(x - previous[1], y - previous[2]); conn.execute("INSERT INTO road_edges(edge_id,map_id,resource_version,from_node_id,to_node_id,road_id,lane_id,length_m,status,source,validation_status,geometry_json,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (edge_id, map_id, resource_version, prev, node_id, road_id, lane_id, length, "OPEN", SOURCE_XODR, UNVERIFIED, json.dumps([[previous[1], previous[2]], [x, y]]), "No traffic-rule or vehicle-clearance validation")); edge_count += 1; road_edges += 1
                previous_by_lane[lane_id] = node_id
        conn.execute("UPDATE road_clusters SET node_count=?, edge_count=? WHERE cluster_id=?", (road_nodes, road_edges, cluster_id))
    for junction in payload["junctions"]:
        junction_id = "{}:{}".format(map_id, junction["id"]); conn.execute("INSERT INTO junctions(junction_id,map_id,resource_version,junction_type,name,validation_status,source,notes) VALUES(?,?,?,?,?,?,?,?)", (junction_id, map_id, resource_version, "JUNCTION", junction.get("name"), UNVERIFIED, SOURCE_XODR, "Connectivity from OpenDRIVE; no CARLA traversal proof"))
        for connection in junction["connections"]:
            for lane_link in connection["lane_links"]: conn.execute("INSERT INTO junction_connections(junction_id,resource_version,connection_id,incoming_road,connecting_road,contact_point,from_lane_id,to_lane_id,source) VALUES(?,?,?,?,?,?,?,?,?)", (junction_id, resource_version, connection.get("id"), connection.get("incoming_road"), connection.get("connecting_road"), connection.get("contact_point"), lane_link["from"], lane_link["to"], SOURCE_XODR))
    conn.execute("UPDATE map_resource_versions SET xodr_hash=?, status=?, notes=? WHERE map_id=? AND resource_version=?", (payload["xodr_hash"], "STATIC_IMPORTED", "Phase 2 static XODR import; CARLA calibration pending", map_id, resource_version)); conn.commit()
    return {"map_id": map_id, "resource_version": resource_version, "roads": len(payload["roads"]), "junctions": len(payload["junctions"]), "nodes": node_count, "edges": edge_count, "points": point_count, "xodr_hash": payload["xodr_hash"], "source": SOURCE_XODR}

# Descriptive aliases kept for callers that prefer class/function naming.
import_static_xodr = import_xodr
parse_open_drive = parse_xodr
