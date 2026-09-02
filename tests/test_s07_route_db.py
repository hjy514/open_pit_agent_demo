import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.map_resources import MapResourceStore, route_plans_from_store
from open_pit_agent.scenario import run_s07_structural_mock


class S07RouteDatabaseTest(unittest.TestCase):
    def test_route_candidates_are_loaded_and_used_by_runner(self):
        config = load_config(PROJECT_ROOT / "configs" / "s07_road_closure_6v.json")
        with tempfile.TemporaryDirectory() as td:
            store = MapResourceStore(Path(td) / "map.db")
            try:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                for point_id in (
                    "inspect-S01-A-from", "inspect-S01-A-to",
                    "inspect-S01-B-from", "inspect-S01-B-to",
                ):
                    store.connection.execute(
                        "INSERT INTO map_points(point_id,map_id,x,y,z,validation_status,source) VALUES(?,?,?,?,?,?,?)",
                        (point_id, "m", 0, 0, 0, "CANDIDATE", "TEST"),
                    )
                for task_id, edge_id in (("inspect-S01-A", "R1"), ("inspect-S01-B", "R2")):
                    store.connection.execute(
                        "INSERT INTO route_candidates(route_candidate_id,map_id,resource_version,from_point_id,to_point_id,candidate_rank,planner_version,route_hash,road_lane_sequence_json,validation_status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, "m", "v", task_id + "-from", task_id + "-to", 1, "test", task_id, json.dumps({"edge_ids": [edge_id]}), "CANDIDATE", "TEST"),
                    )
                store.connection.commit()
                plans = route_plans_from_store(
                    store,
                    "m",
                    "v",
                    {"inspect-S01-A": ("inspect-S01-A-from", "inspect-S01-A-to")},
                )
                self.assertEqual({"inspect-S01-A": ["R1"]}, plans)
                result = run_s07_structural_mock(
                    config,
                    map_resource_store=store,
                    map_id="m",
                    resource_version="v",
                    route_endpoints={
                        "inspect-S01-A": ("inspect-S01-A-from", "inspect-S01-A-to"),
                        "inspect-S01-B": ("inspect-S01-B-from", "inspect-S01-B-to"),
                    },
                )
                self.assertEqual("map_resource_db", result["route_source"])
                self.assertEqual(["inspect-S01-A"], result["route_impact_task_ids"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
