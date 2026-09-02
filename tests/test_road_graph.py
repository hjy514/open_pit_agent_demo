import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore, RoadGraph, identify_affected_routes


class RoadGraphTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
