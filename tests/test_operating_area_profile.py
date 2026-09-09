import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.scenario.operating_areas import (
    area_records, load_operating_area_profile, register_operating_area_profile,
)


class OperatingAreaProfileTest(unittest.TestCase):
    def test_profile_preserves_candidate_and_physical_boundaries(self):
        profile = load_operating_area_profile(
            PROJECT_ROOT / "configs" / "map_resource_operating_areas_v1.json"
        )
        records = list(area_records(profile))
        statuses = {item["area_id"]: item["validation_status"] for item in records}
        self.assertEqual("CANDIDATE_ONLY", statuses["low_bench_six_vehicle_candidate_pool"])
        self.assertEqual("PHYSICAL_ROUTE_ANCHORED", statuses["h1_review_wait"])
        self.assertEqual("SYNTHETIC_HAZARD_ANCHOR", statuses["east_slope_hazard_anchor"])

    def test_registration_requires_known_map_points(self):
        profile = load_operating_area_profile(
            PROJECT_ROOT / "configs" / "map_resource_operating_areas_v1.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "resources.db") as store:
                store.initialise_map("0325_5", "0325_5", "0325_5", "vehicle.cat.cat")
                indices = sorted({
                    int(point_id.split(":")[1])
                    for record in area_records(profile)
                    for point_id in record["point_ids"]
                })
                store.upsert_spawn_calibration_points("0325_5", [
                    {"spawn_point_index": index, "x": float(index), "y": 0.0,
                     "z": 0.0, "validation_status": "VERIFIED_SPAWN", "source": "TEST"}
                    for index in indices
                ])
                self.assertEqual(9, register_operating_area_profile(store, profile))
                stored = list(store.operating_areas("0325_5", "1.0-draft"))
                self.assertEqual(9, len(stored))
                self.assertEqual(19, len(store.table_names()))


if __name__ == "__main__":
    unittest.main()
