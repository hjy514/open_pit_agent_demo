import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.mock_adapter import MockAdapter
from open_pit_agent.config import load_config
from open_pit_agent.scheduler import (
    BaselineScheduler,
    release_failed_vehicle_tasks,
    tasks_from_zones,
)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        self.adapter = MockAdapter(self.config)
        self.adapter.connect()
        self.scheduler = BaselineScheduler()

    def tearDown(self):
        self.adapter.close()

    def test_initial_schedule_uses_three_vehicles(self):
        tasks = tasks_from_zones(self.config.zones)
        assignments = self.scheduler.assign(
            tasks, self.adapter.list_states(), self.config.zones
        )

        self.assertEqual(3, len(assignments))
        self.assertEqual(3, len({item.vehicle_id for item in assignments}))

    def test_failed_vehicle_task_is_reassigned(self):
        tasks = tasks_from_zones(self.config.zones)
        self.scheduler.assign(tasks, self.adapter.list_states(), self.config.zones)
        failed = self.config.demo.failure_vehicle_id
        failed_task_ids = {
            task.task_id
            for task in tasks
            if task.assigned_vehicle_id == failed
        }
        self.assertTrue(failed_task_ids)

        self.adapter.inject_fault(failed)
        released = set(release_failed_vehicle_tasks(tasks, failed))
        reassigned = self.scheduler.assign(
            tasks,
            self.adapter.list_states(),
            self.config.zones,
            excluded_vehicle_ids={failed},
        )

        self.assertEqual(failed_task_ids, released)
        self.assertEqual(failed_task_ids, {item.task_id for item in reassigned})
        self.assertNotIn(failed, {item.vehicle_id for item in reassigned})


if __name__ == "__main__":
    unittest.main()

