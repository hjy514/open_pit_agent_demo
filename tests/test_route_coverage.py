import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.map_resources.route_coverage import constrained_seeded_tasks, seeded_task_candidates
from open_pit_agent.scenario.generator import (
    EpisodeGenerationError, _annotate_task_semantics,
    _assign_deadhead_staging_points,
    _semantic_seeded_tasks, build_s02_recoverable_task_drafts,
    select_execution_route_facts,
)
from scripts.validate_physical_routes import (
    candidate_selection_summary, select_complexity_candidates,
)


class RouteCoverageTests(unittest.TestCase):
    def test_p6_complexity_candidate_selection_is_seeded_and_keeps_overlap(self):
        candidates = [
            {
                "from_point_id": "p{}".format(index),
                "to_point_id": target,
                "route_length_m": 200 + index,
                "junction_count": index % 3,
                "edge_ids": edges,
                "semantic_endpoint": index % 2 == 0,
            }
            for index, (target, edges) in enumerate((
                ("shared-target", ["a", "shared-edge", "b"]),
                ("shared-target", ["c", "shared-edge", "d"]),
                ("t2", ["e", "shared-edge", "f"]),
                ("t3", ["g", "other-edge", "h"]),
                ("t4", ["i", "other-edge", "j"]),
                ("t5", ["k", "last-edge", "l"]),
            ))
        ]
        first = select_complexity_candidates(
            candidates, 4, 202616, set()
        )
        second = select_complexity_candidates(
            candidates, 4, 202616, set()
        )
        self.assertEqual(first, second)
        summary = candidate_selection_summary(first)
        self.assertEqual(4, summary["route_count"])
        self.assertEqual(4, summary["unique_origin_count"])
        self.assertGreaterEqual(summary["shared_destination_route_count"], 2)
        self.assertGreaterEqual(summary["topology_overlap_edge_count"], 1)

    def test_deadhead_staging_is_seeded_and_keeps_business_route(self):
        tasks = [{
            "task_id": "task-1", "vehicle_id": "truck-1",
            "from_point_id": "load-a", "to_point_id": "dump-a",
        }]
        routes = [{
            "from_point_id": "staging-a", "to_point_id": "load-a",
            "route_length_m": 240.0,
            "validation_status": "PLANNER_REACHABLE",
        }]
        points = {
            point_id: {"point_id": point_id}
            for point_id in ("staging-a", "load-a", "dump-a")
        }
        first = _assign_deadhead_staging_points(
            tasks, routes, points, set(), seed=202601,
        )
        second = _assign_deadhead_staging_points(
            tasks, routes, points, set(), seed=202601,
        )

        self.assertEqual(first, second)
        self.assertEqual("staging-a", first[0]["spawn_point_id"])
        self.assertEqual("load-a", first[0]["service_origin_point_id"])
        self.assertEqual("dump-a", first[0]["service_target_point_id"])
        self.assertTrue(first[0]["requires_deadhead"])
        self.assertEqual(240.0, first[0]["deadhead_route_length_m"])

    def test_semantic_sampler_selects_missions_before_routes(self):
        roles = ["haul", "haul", "haul", "inspection", "inspection", "support"]
        pairs = [
            ("load-a", "dump-a"), ("load-b", "dump-b"),
            ("load-c", "dump-c"), ("start-i1", "inspect-a"),
            ("start-i2", "inspect-b"), ("start-s", "support-a"),
        ]
        routes = [
            {
                "from_point_id": source, "to_point_id": target,
                "route_length_m": 800, "planner_version": "test",
            }
            for source, target in pairs
        ]
        areas = [
            {
                "area_id": "loaders", "area_type": "haul_loading",
                "selection_mode": "seeded_random", "allowed_roles": ["haul"],
                "point_ids": ["load-a", "load-b", "load-c"],
            },
            {
                "area_id": "dumps", "area_type": "haul_dump",
                "selection_mode": "seeded_random", "allowed_roles": ["haul"],
                "point_ids": ["dump-a", "dump-b", "dump-c"],
            },
            {
                "area_id": "inspection", "area_type": "inspection_task_core",
                "selection_mode": "seeded_random", "allowed_roles": ["inspection"],
                "point_ids": ["inspect-a", "inspect-b"],
            },
            {
                "area_id": "support", "area_type": "support_task_core",
                "selection_mode": "seeded_random", "allowed_roles": ["support"],
                "point_ids": ["support-a"],
            },
        ]
        first = _semantic_seeded_tasks(
            routes, set(), 31, 6, 500, 1000, "s01", areas,
        )
        self.assertEqual(
            first,
            _semantic_seeded_tasks(
                routes, set(), 31, 6, 500, 1000, "s01", areas,
            ),
        )
        self.assertEqual(roles, [item["vehicle_role"] for item in first])
        annotated = _annotate_task_semantics(
            first, areas, "SEMANTIC_AREA_CONSTRAINED"
        )
        self.assertTrue(all(
            item["origin_area_type"] == "haul_loading"
            and item["target_area_type"] == "haul_dump"
            for item in annotated[:3]
        ))
        self.assertTrue(all(
            item["target_area_type"] == "inspection_task_core"
            for item in annotated[3:5]
        ))
        self.assertEqual("support_task_core", annotated[5]["target_area_type"])

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

    def test_constrained_tasks_conservatively_avoid_static_spawn_candidates(self):
        routes = [{"from_point_id": "p{}".format(a), "to_point_id": "q{}".format(a),
                   "route_length_m": 800, "endpoint_error_m": 1, "junction_count": 0,
                   "planner_version": "test"}
                  for a in range(7)]
        tasks = constrained_seeded_tasks(
            routes, set(), 3, 6, 500, 1000,
            avoid_spawn_pairs={("p0", "p1")},
        )
        origins = {item["from_point_id"] for item in tasks}
        self.assertFalse({"p0", "p1"}.issubset(origins))

    def test_s02_generator_reserves_a_route_level_takeover_candidate(self):
        pairs = [("a", "t"), ("b", "t"), ("b", "d"), ("c", "e"),
                 ("f", "g"), ("h", "i"), ("j", "k")]
        routes = [{"from_point_id": source, "to_point_id": target,
                   "route_length_m": 800, "planner_version": "test"}
                  for source, target in pairs]
        tasks, metadata = build_s02_recoverable_task_drafts(
            routes, set(), seed=17, vehicle_count=6,
            minimum_length_m=500, maximum_length_m=1000,
        )
        self.assertEqual(6, len(tasks))
        self.assertEqual("inspection", tasks[0]["vehicle_role"])
        self.assertEqual("inspection", tasks[1]["vehicle_role"])
        self.assertEqual(tasks[0]["vehicle_id"], metadata["failed_vehicle_id"])
        self.assertEqual(tasks[1]["vehicle_id"], metadata["takeover_candidate_vehicle_ids"][0])
        self.assertEqual(tasks[0]["to_point_id"], metadata["fallback_route"]["to_point_id"])
        self.assertEqual(tasks[1]["from_point_id"], metadata["fallback_route"]["from_point_id"])
        self.assertNotEqual(tasks[1]["to_point_id"], tasks[0]["to_point_id"])

    def test_s02_generator_fails_closed_without_a_takeover_route(self):
        routes = [{"from_point_id": "p{}".format(index),
                   "to_point_id": "q{}".format(index),
                   "route_length_m": 800, "planner_version": "test"}
                  for index in range(6)]
        with self.assertRaisesRegex(EpisodeGenerationError, "NO_TAKEOVER_CANDIDATE"):
            build_s02_recoverable_task_drafts(
                routes, set(), seed=17, vehicle_count=6,
                minimum_length_m=500, maximum_length_m=1000,
            )

    def test_s02_semantic_generator_keeps_takeover_and_role_areas(self):
        pairs = [
            ("a", "inspect-a"), ("b", "inspect-a"),
            ("b", "inspect-b"), ("load-a", "dump-a"),
            ("load-b", "dump-b"), ("load-c", "dump-c"),
            ("support-start", "support-a"),
        ]
        routes = [
            {
                "from_point_id": source, "to_point_id": target,
                "route_length_m": 800, "planner_version": "test",
            }
            for source, target in pairs
        ]
        areas = [
            {
                "area_id": "loaders", "area_type": "haul_loading",
                "selection_mode": "seeded_random", "allowed_roles": ["haul"],
                "point_ids": ["load-a", "load-b", "load-c"],
            },
            {
                "area_id": "dumps", "area_type": "haul_dump",
                "selection_mode": "seeded_random", "allowed_roles": ["haul"],
                "point_ids": ["dump-a", "dump-b", "dump-c"],
            },
            {
                "area_id": "inspection", "area_type": "inspection_task_core",
                "selection_mode": "seeded_random", "allowed_roles": ["inspection"],
                "point_ids": ["inspect-a", "inspect-b"],
            },
            {
                "area_id": "support", "area_type": "support_task_core",
                "selection_mode": "seeded_random", "allowed_roles": ["support"],
                "point_ids": ["support-a"],
            },
        ]
        tasks, metadata = build_s02_recoverable_task_drafts(
            routes, set(), seed=17, vehicle_count=6,
            minimum_length_m=500, maximum_length_m=1000,
            operating_areas=areas,
        )
        annotated = _annotate_task_semantics(
            tasks, areas, "SEMANTIC_AREA_AND_RECOVERY_CONSTRAINED"
        )
        self.assertEqual(
            annotated[0]["to_point_id"],
            metadata["fallback_route"]["to_point_id"],
        )
        self.assertTrue(all(
            item["origin_area_type"] == "haul_loading"
            and item["target_area_type"] == "haul_dump"
            for item in annotated[2:5]
        ))
        self.assertEqual("support_task_core", annotated[5]["target_area_type"])

    def test_p6_route_keeps_non_strict_p5_provenance(self):
        strict = [{"from_point_id": "a", "to_point_id": "t",
                   "route_length_m": 200, "validation_status": "PLANNER_REACHABLE"}]
        all_facts = strict + [{"from_point_id": "b", "to_point_id": "d",
                               "route_length_m": 250,
                               "validation_status": "PLANNER_ENDPOINT_MISMATCH"}]
        selected = select_execution_route_facts(
            strict, all_facts, {("a", "t"), ("b", "d")}
        )
        by_pair = {(item["from_point_id"], item["to_point_id"]): item
                   for item in selected}
        self.assertEqual("P6_PHYSICAL_REACHED", by_pair[("a", "t")]["validation_status"])
        self.assertEqual("PLANNER_REACHABLE", by_pair[("a", "t")]["planner_validation_status"])
        self.assertEqual("P6_PHYSICAL_REACHED_WITH_PLANNER_ENDPOINT_MISMATCH",
                         by_pair[("b", "d")]["validation_status"])


if __name__ == "__main__":
    unittest.main()
