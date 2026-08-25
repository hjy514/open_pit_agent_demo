import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_data import build_map_payload


class FakeLocation:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class FakeTransform:
    def __init__(self, x, y):
        self.location = FakeLocation(x, y)


class FakeWaypoint:
    def __init__(
        self, road_id, section_id, lane_id, s, x, y
    ):
        self.road_id = road_id
        self.section_id = section_id
        self.lane_id = lane_id
        self.s = s
        self.transform = FakeTransform(x, y)


class MapDataTest(unittest.TestCase):
    def test_waypoints_become_sorted_lane_polylines(self):
        payload = build_map_payload(
            "Town03",
            [
                FakeWaypoint(1, 0, -1, 10, 10, 2),
                FakeWaypoint(1, 0, -1, 0, 0, 2),
                FakeWaypoint(2, 0, 1, 0, -5, -3),
                FakeWaypoint(2, 0, 1, 5, -1, -3),
            ],
            waypoint_distance_m=5.0,
        )

        self.assertEqual("Town03", payload["map_name"])
        self.assertEqual(2, payload["polyline_count"])
        self.assertEqual(
            [[0.0, 2.0], [10.0, 2.0]],
            payload["polylines"][0]["points"],
        )
        self.assertEqual(-5.0, payload["bounds"]["min_x"])
        self.assertEqual(10.0, payload["bounds"]["max_x"])


if __name__ == "__main__":
    unittest.main()
