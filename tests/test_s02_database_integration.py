import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import run_s02_structural_mock
from open_pit_agent.sqlite_store import SqliteRunStore


class S02DatabaseIntegrationTest(unittest.TestCase):
    def test_fault_and_takeover_events_are_recorded(self):
        config = load_config(PROJECT_ROOT / "configs" / "s02_vehicle_failure_6v.json")
        with tempfile.TemporaryDirectory() as td:
            store = SqliteRunStore(Path(td) / "openpit.db")
            try:
                result = run_s02_structural_mock(config, seed=202602, run_store=store)
                self.assertEqual("PASS", result["status"])
                rows = store.connection.execute(
                    "SELECT event_type, count(*) FROM events WHERE run_id=? GROUP BY event_type",
                    ("s02-structural-mock",),
                ).fetchall()
                events = dict(rows)
                self.assertEqual(1, events.get("vehicle_fault"))
                self.assertGreaterEqual(events.get("task_released", 0), 1)
                self.assertGreaterEqual(events.get("task_reassigned", 0), 1)
                self.assertEqual(6, events.get("task_completed"))
                self.assertEqual(1, events.get("run_completed"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
