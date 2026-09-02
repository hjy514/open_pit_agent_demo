import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore


class MapResourceStoreTests(unittest.TestCase):
    def test_schema_and_metadata_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "map_resources.db"
            with MapResourceStore(database_path) as store:
                store.initialise_map(
                    map_id="0325_5",
                    map_name="0325_5",
                    carla_map_name="0325_5",
                    vehicle_blueprint="vehicle.cat.cat",
                )
                store.initialise_map(
                    map_id="0325_5",
                    map_name="0325_5",
                    carla_map_name="0325_5",
                    vehicle_blueprint="vehicle.cat.cat",
                )
                validation = store.validate_schema()
                self.assertTrue(validation["valid"])
                self.assertEqual([], validation["missing_tables"])
                maps = store.connection.execute("SELECT count(*) FROM maps").fetchone()[0]
                versions = store.connection.execute(
                    "SELECT count(*) FROM map_resource_versions"
                ).fetchone()[0]
                points = store.connection.execute(
                    "SELECT count(*) FROM map_points"
                ).fetchone()[0]
                self.assertEqual(1, maps)
                self.assertEqual(1, versions)
                self.assertEqual(0, points)

    def test_point_role_foreign_key_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with MapResourceStore(Path(temporary_directory) / "map_resources.db") as store:
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        "INSERT INTO point_roles(point_id, role, source) VALUES (?, ?, ?)",
                        ("missing-point", "SPAWN", "TEST"),
                    )


if __name__ == "__main__":
    unittest.main()
