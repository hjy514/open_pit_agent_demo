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
    release_hazard_affected_tasks,
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

    def test_hazard_safe_hold_releases_traceable_takeover_task(self):
        tasks = tasks_from_zones(self.config.zones)
        self.scheduler.assign(
            tasks, self.adapter.list_states(), self.config.zones
        )
        affected = self.config.demo.failure_vehicle_id
        affected_task = next(
            task for task in tasks
            if task.assigned_vehicle_id == affected
        )

        released = release_hazard_affected_tasks(
            tasks, affected, tick=520
        )

        self.assertEqual([affected_task.task_id], released)
        self.assertEqual(affected, affected_task.original_vehicle_id)
        self.assertEqual(520, affected_task.handover_tick)
        self.assertEqual(
            "released_after_slope_hazard_safe_hold",
            affected_task.handover_reason,
        )

    def test_mine_routine_task_starts_on_owner_then_allows_takeover(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = MockAdapter(config)
        adapter.connect()
        try:
            tasks = tasks_from_zones(config.zones)
            routine_task = next(
                task
                for task in tasks
                if task.zone_id == "routine_zone_01"
            )
            scheduler = BaselineScheduler()
            scheduler.assign(tasks, adapter.list_states(), config.zones)
            self.assertEqual(
                "inspection_vehicle_01",
                routine_task.assigned_vehicle_id,
            )

            release_hazard_affected_tasks(
                tasks, "inspection_vehicle_01", tick=520
            )
            scheduler.assign(
                [routine_task],
                adapter.list_states(),
                config.zones,
                excluded_vehicle_ids={"inspection_vehicle_01"},
            )
            self.assertEqual(
                "inspection_vehicle_02",
                routine_task.assigned_vehicle_id,
            )
        finally:
            adapter.close()

    def test_mine_takeover_prefers_idle_h1_standby_candidate(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = MockAdapter(config)
        adapter.connect()
        try:
            scheduler = BaselineScheduler()
            tasks = tasks_from_zones(config.zones)
            states = adapter.list_states()
            initial_assignments = scheduler.assign(
                tasks, states, config.zones
            )
            self.assertEqual(2, len(initial_assignments))
            self.assertEqual(
                {
                    "routine_zone_01": "inspection_vehicle_01",
                    "southern_transport_patrol_zone_03": (
                        "emergency_vehicle_01"
                    ),
                },
                {
                    item.zone_id: item.vehicle_id
                    for item in initial_assignments
                },
            )
            affected_task = next(
                task for task in tasks
                if task.assigned_vehicle_id == "inspection_vehicle_01"
            )
            release_hazard_affected_tasks(
                tasks, "inspection_vehicle_01", tick=520
            )

            candidates = scheduler.rank_candidates(
                affected_task,
                states,
                config.zones,
                active_tasks=tasks,
                excluded_vehicle_ids={"inspection_vehicle_01"},
            )

            self.assertEqual(
                {"inspection_vehicle_02", "emergency_vehicle_01"},
                {item.vehicle_id for item in candidates},
            )
            candidate_reasons = {
                item.vehicle_id: item.reason for item in candidates
            }
            self.assertIn("active_load=0", candidate_reasons["inspection_vehicle_02"])
            self.assertIn("active_load=1", candidate_reasons["emergency_vehicle_01"])
            self.assertEqual("inspection_vehicle_02", candidates[0].vehicle_id)
        finally:
            adapter.close()


if __name__ == "__main__":
    unittest.main()
