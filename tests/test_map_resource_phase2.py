import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from open_pit_agent.map_resources import MapResourceStore, parse_xodr, SOURCE_XODR


class XodrPhase2Test(unittest.TestCase):
    def test_import_is_static_and_idempotent(self):
        xml = '''<OpenDRIVE xmlns="urn:test"><road id="1" length="10"><planView><geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry></planView><lanes><laneSection s="0"><center><lane id="0" type="none"/></center></laneSection></lanes></road></OpenDRIVE>'''
        with tempfile.TemporaryDirectory() as td:
            xodr = Path(td) / "map.xodr"
            xodr.write_text(xml, encoding="utf-8")
            self.assertEqual(1, len(parse_xodr(xodr)["roads"]))
            with MapResourceStore(Path(td) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="2.0")
                first = store.import_xodr("m", "2.0", xodr, spacing_m=5)
                second = store.import_xodr("m", "2.0", xodr, spacing_m=5)
                self.assertEqual(first["xodr_hash"], second["xodr_hash"])
                self.assertEqual(3, store.connection.execute("SELECT count(*) FROM road_nodes").fetchone()[0])
                self.assertEqual(SOURCE_XODR, store.connection.execute("SELECT source FROM map_points LIMIT 1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
