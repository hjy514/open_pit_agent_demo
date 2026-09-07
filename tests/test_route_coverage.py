import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.map_resources.route_coverage import constrained_seeded_tasks, seeded_task_candidates


class RouteCoverageTests(unittest.TestCase):
    def test_seeded_tasks_are_reproducible_and_distinct(self):
        routes = [{"from_point_id": "p{}".format(a), "to_point_id": "p{}".format(b),
                   "route_length_m": 10, "endpoint_error_m": 1, "junction_count": 0,
                   "planner_version": "test"}
                  for a, b in ((0, 11), (1, 12), (2, 13), (3, 14), (4, 15), (5, 16))]
        first = seeded_task_candidates(routes, 7, 6)
        self.assertEqual(first, seeded_task_candidates(routes, 7, 6))
        self.assertEqual(6, len({item["from_point_id"] for item in first}))
        self.assertEqual(6, len({item["to_point_id"] for item in first}))

    def test_constrained_tasks_keep_roles_and_length_window(self):
        routes = [{"from_point_id": "p{}".format(a), "to_point_id": "p{}".format(b),
                   "route_length_m": 800, "endpoint_error_m": 1, "junction_count": 0,
                   "planner_version": "test"}
                  for a, b in ((0, 11), (1, 12), (2, 13), (3, 14), (4, 15), (5, 16))]
        tasks = constrained_seeded_tasks(routes, {("p0", "p99")}, 9, 6, 500, 1000)
        self.assertEqual(6, len(tasks))
        self.assertEqual(["haul", "haul", "haul", "inspection", "inspection", "support"],
                         [item["vehicle_role"] for item in tasks])
        self.assertTrue(all(500 <= item["route_length_m"] <= 1000 for item in tasks))
        self.assertFalse(
            {item["from_point_id"] for item in tasks}.intersection(
                item["to_point_id"] for item in tasks
            )
        )


if __name__ == "__main__":
    unittest.main()
