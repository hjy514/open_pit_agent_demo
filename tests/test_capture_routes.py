import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "capture_carla_routes.py"
SPEC = importlib.util.spec_from_file_location("capture_carla_routes", str(MODULE_PATH))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CaptureRoutesTest(unittest.TestCase):
    def test_endpoint_loader_accepts_scenario_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "endpoints.json"
            path.write_text(json.dumps({"route_endpoints": {"task-a": {"from_spawn_point_index": 1, "to_spawn_point_index": 4}}}), encoding="utf-8")
            self.assertEqual(
                {"task-a": {"from_spawn_point_index": 1, "to_spawn_point_index": 4, "from_point_id": None, "to_point_id": None}},
                MODULE.load_route_endpoints(path),
            )

    def test_route_payload_is_deterministic_without_carla_import(self):
        class Location:
            def __init__(self, x, y, z=0):
                self.x, self.y, self.z = x, y, z

        class Transform:
            def __init__(self, location):
                self.location = location

        class Waypoint:
            def __init__(self, x, y):
                self.transform = Transform(Location(x, y))
                self.road_id = 7
                self.lane_id = -1
                self.s = x
                self.is_junction = False

        sequence, length, route_hash = MODULE.route_payload([(Waypoint(0, 0), None), (Waypoint(3, 4), None)])
        self.assertEqual(5.0, length)
        self.assertEqual(64, len(route_hash))
        self.assertEqual(route_hash, MODULE.route_payload([(Waypoint(0, 0), None), (Waypoint(3, 4), None)])[2])
        self.assertEqual([], sequence["edge_ids"])


if __name__ == "__main__":
    unittest.main()
