import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import (
    GraphEdge, MapResourceStore, RoadGraph, RoutePlanner,
    derive_topology_route_candidates,
    identify_affected_routes,
)


class RoadGraphTest(unittest.TestCase):
    @staticmethod
    def _insert_node(store, node_id, x, y):
        store.connection.execute(
            "INSERT INTO road_nodes(node_id,map_id,resource_version,x,y,z,node_type,source,validation_status) VALUES(?,?,?,?,?,?,?,?,?)",
            (node_id, "m", "v", x, y, 0, "TEST", "TEST", "CANDIDATE"),
        )

    @staticmethod
    def _insert_edge(store, edge_id, source, target, length, geometry):
        store.connection.execute(
            "INSERT INTO road_edges(edge_id,map_id,resource_version,from_node_id,to_node_id,road_id,lane_id,length_m,status,source,validation_status,geometry_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id, "m", "v", source, target, "1", 1, length, "OPEN", "TEST", "CANDIDATE", json.dumps(geometry)),
        )

    def test_route_impact_uses_closed_edge_intersection(self):
        self.assertEqual(
            ["task-a"],
            identify_affected_routes({"task-a": ["e1", "e2"], "task-b": ["e3"]}, {"e2"}),
        )

    def test_shortest_path_and_closed_edge(self):
        with tempfile.TemporaryDirectory() as td:
            store = MapResourceStore(Path(td) / "map.db")
            try:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                for node in ("a", "b", "c", "d"):
                    store.connection.execute("INSERT INTO road_nodes(node_id,map_id,resource_version,x,y,z,node_type,source,validation_status) VALUES(?,?,?,?,?,?,?,?,?)", (node, "m", "v", 0, 0, 0, "TEST", "TEST", "CANDIDATE"))
                edges = [("e1", "a", "b", 5), ("e2", "b", "d", 5), ("e3", "a", "c", 2), ("e4", "c", "d", 20)]
                for edge_id, source, target, length in edges:
                    store.connection.execute("INSERT INTO road_edges(edge_id,map_id,resource_version,from_node_id,to_node_id,length_m,status,source,validation_status) VALUES(?,?,?,?,?,?,?,?,?)", (edge_id, "m", "v", source, target, length, "OPEN", "TEST", "CANDIDATE"))
                store.connection.commit()
                graph = RoadGraph.from_store(store, "m", "v")
                route = graph.shortest_path("a", "d")
                self.assertTrue(route["reachable"])
                self.assertEqual(["e1", "e2"], route["edge_ids"])
                blocked = graph.shortest_path("a", "d", closed_edge_ids={"e2"})
                self.assertEqual(["e3", "e4"], blocked["edge_ids"])
                self.assertFalse(graph.shortest_path("d", "a")["reachable"])
            finally:
                store.close()

    def test_route_planner_returns_common_contract_and_honors_road_state(self):
        graph = RoadGraph([GraphEdge("e1", "a", "b", 100.0)])
        planner = RoutePlanner(graph)
        start = {"point_id": "p1", "edge_id": "e1", "fraction": 0.1}
        goal = {"point_id": "p2", "edge_id": "e1", "fraction": 0.9}
        route = planner.plan(start, goal).to_dict()
        self.assertEqual("openpit.route-plan.v1", route["schema_version"])
        self.assertEqual("PLANNED", route["planning_status"])
        self.assertEqual(["e1"], route["edge_ids"])
        self.assertAlmostEqual(80.0, route["distance_m"])
        blocked = planner.plan(
            start, goal, road_state={"e1": "CLOSED"}
        ).to_dict()
        self.assertEqual("UNREACHABLE", blocked["planning_status"])
        self.assertEqual(["e1"], blocked["closed_edge_ids"])
        risk_blocked = planner.plan(
            start, goal, risk_state={"prohibited_edge_ids": ["e1"]}
        ).to_dict()
        self.assertEqual("UNREACHABLE", risk_blocked["planning_status"])
        self.assertEqual(["e1"], risk_blocked["risk_blocked_edge_ids"])

    def test_point_binding_uses_segment_projection(self):
        with tempfile.TemporaryDirectory() as td:
            with MapResourceStore(Path(td) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                self._insert_node(store, "a", 0, 0)
                self._insert_node(store, "b", 100, 0)
                self._insert_edge(store, "e1", "a", "b", 100, [[0, 0, 0], [100, 0, 0]])
                store.connection.commit()
                anchor = RoadGraph.from_store(store, "m", "v").bind_point({
                    "point_id": "p", "x": 50, "y": 3, "z": 0,
                    "road_id": "1", "lane_id": 1,
                })
                self.assertAlmostEqual(3.0, anchor["projection_distance_m"])
                self.assertAlmostEqual(0.5, anchor["fraction"])

    def test_derives_route_candidate_only_for_strict_p5_pair(self):
        with tempfile.TemporaryDirectory() as td:
            with MapResourceStore(Path(td) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                self._insert_node(store, "a", 0, 0)
                self._insert_node(store, "b", 100, 0)
                self._insert_edge(store, "e1", "a", "b", 100, [[0, 0, 0], [100, 0, 0]])
                for point_id, x in (("p1", 10), ("p2", 90)):
                    store.connection.execute(
                        "INSERT INTO map_points(point_id,map_id,x,y,z,road_id,lane_id,validation_status,source) VALUES(?,?,?,?,?,?,?,?,?)",
                        (point_id, "m", x, 0, 0, "1", 1, "VERIFIED_SPAWN", "TEST"),
                    )
                store.connection.execute(
                    "INSERT INTO reachable_pairs(map_id,resource_version,from_point_id,to_point_id,planner_version,reachable,route_length_m,validation_status,source) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("m", "v", "p1", "p2", "test", 1, 80, "PLANNER_REACHABLE", "TEST"),
                )
                store.connection.commit()
                result = derive_topology_route_candidates(store, "m", "v", 1.0)
                self.assertEqual(2, result["anchored_point_count"])
                self.assertEqual(1, result["route_candidate_count"])
                row = store.connection.execute(
                    "SELECT road_lane_sequence_json,validation_status FROM route_candidates"
                ).fetchone()
                self.assertEqual(["e1"], json.loads(row[0])["edge_ids"])
                self.assertEqual("TOPOLOGY_DERIVED_UNVERIFIED", row[1])


if __name__ == "__main__":
    unittest.main()
