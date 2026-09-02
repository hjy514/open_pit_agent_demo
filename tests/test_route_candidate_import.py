import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore, route_plans_from_store


class RouteCandidateImportTest(unittest.TestCase):
    def test_upsert_is_idempotent_and_sequence_is_serialized(self):
        with tempfile.TemporaryDirectory() as td:
            with MapResourceStore(Path(td) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                for point_id in ("from", "to"):
                    store.connection.execute(
                        "INSERT INTO map_points(point_id,map_id,x,y,z,validation_status,source) VALUES(?,?,?,?,?,?,?)",
                        (point_id, "m", 0, 0, 0, "CANDIDATE", "TEST"),
                    )
                record = {
                    "route_candidate_id": "r1",
                    "map_id": "m",
                    "resource_version": "v",
                    "from_point_id": "from",
                    "to_point_id": "to",
                    "road_lane_sequence": {"edge_ids": ["E1", "E2"]},
                }
                self.assertEqual(1, store.upsert_route_candidates([record]))
                self.assertEqual(1, store.upsert_route_candidates([record]))
                plans = route_plans_from_store(store, "m", "v", {"task": ("from", "to")})
                self.assertEqual({"task": ["E1", "E2"]}, plans)
                count = store.connection.execute("SELECT COUNT(*) FROM route_candidates").fetchone()[0]
                self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
