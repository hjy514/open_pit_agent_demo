import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore, import_carla_topology


class _Location:
    def __init__(self, x, y, z=0.0): self.x, self.y, self.z = x, y, z


class _Transform:
    def __init__(self, location): self.location = location


class _Waypoint:
    def __init__(self, road, lane, s, x, y, z=0.0):
        self.road_id, self.lane_id, self.s = road, lane, s
        self.transform = _Transform(_Location(x, y, z))


class _Map:
    def get_topology(self):
        return [
            (_Waypoint(1, 1, 0, 0, 0), _Waypoint(1, 1, 10, 10, 0)),
            (_Waypoint(1, 1, 10, 10, 0), _Waypoint(2, 1, 0, 10, 10)),
        ]


class CarlaTopologyImportTest(unittest.TestCase):
    def test_import_is_idempotent_and_directed(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                first = import_carla_topology(store, _Map(), "m", "v")
                second = import_carla_topology(store, _Map(), "m", "v")
                self.assertEqual(2, first["topology_pair_count"])
                self.assertEqual(3, first["node_count"])
                self.assertEqual(2, first["edge_count"])
                self.assertEqual(first, second)
                self.assertEqual(2, store.connection.execute("SELECT count(*) FROM road_edges").fetchone()[0])
                self.assertEqual(3, store.connection.execute("SELECT count(*) FROM road_nodes").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
