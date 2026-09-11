import json
import sqlite3
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.evidence import EvidenceRecorder
from open_pit_agent.config import load_config
from open_pit_agent.scenario import build_episode
from open_pit_agent.sqlite_store import SqliteRunStore


class SqliteEvidenceStoreTest(unittest.TestCase):
    def test_recorder_close_marks_only_unfinished_run_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "lifecycle-test"
            )
            run_id = recorder.run_id
            database_path = recorder.database_path
            recorder.close()
            connection = sqlite3.connect(str(database_path))
            try:
                row = connection.execute(
                    "SELECT status,ended_at FROM scenario_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            finally:
                connection.close()
        self.assertEqual("INCOMPLETE", row[0])
        self.assertIsNotNone(row[1])

    def test_database_health_and_explicit_stale_repair_preserve_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "openpit.db"
            store = SqliteRunStore(path)
            try:
                store.start_run("old-run", "s01")
                store.start_run("new-run", "s01")
                store.connection.execute(
                    "UPDATE scenario_runs SET started_at=? WHERE run_id='old-run'",
                    ("2000-01-01T00:00:00+00:00",),
                )
                store.connection.commit()
                before = store.database_health_report(stale_after_hours=24)
                repaired = store.mark_stale_runs_incomplete(
                    stale_after_hours=24
                )
                after = store.database_health_report(stale_after_hours=24)
                total = store.connection.execute(
                    "SELECT count(*) FROM scenario_runs"
                ).fetchone()[0]
                new_status = store.connection.execute(
                    "SELECT status FROM scenario_runs WHERE run_id='new-run'"
                ).fetchone()[0]
            finally:
                store.close()
        self.assertEqual(1, before["stale_unfinished_run_count"])
        self.assertEqual(["old-run"], repaired)
        self.assertEqual(0, after["stale_unfinished_run_count"])
        self.assertEqual(2, total)
        self.assertEqual("running", new_status)
        self.assertFalse(before["actions"]["automatic_deletion_performed"])

    def test_closed_loop_cycle_is_stored_as_one_queryable_transition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "cycle-test"
            )
            cycle = {
                "schema_version": "openpit.closed-loop-cycle.v1",
                "cycle_id": "cycle:0", "status": "SUCCEEDED",
                "revision_before": 0, "revision_after": 2,
                "state_before": {
                    "schema_version": "openpit.world-state.v1",
                    "tasks": [{"task_id": "task-1", "status": "pending"}],
                },
                "stage_results": {
                    "risk": {"level": "BLUE"},
                    "decision": {"action": "ASSIGN"},
                    "scheduling": {
                        "command": {
                            "schema_version": "openpit.execution-command.v1",
                            "command_id": "command-1",
                        }
                    },
                    "planning": {"status": "P5_ROUTE_FACTS_ADMITTED"},
                    "safety": {"status": "APPROVED"},
                },
                "execution_feedback": [{
                    "schema_version": "openpit.execution-feedback.v1",
                    "command_id": "command-1", "status": "SUCCEEDED",
                    "physical_execution": False,
                    "measurement_status": "STRUCTURAL_ONLY_NO_PHYSICS",
                }],
                "next_state": {
                    "schema_version": "openpit.world-state.v1",
                    "tasks": [{"task_id": "task-1", "status": "completed"}],
                },
                "trace": [{"stage": "state"}, {"stage": "feedback"}],
            }
            try:
                count = recorder.record_closed_loop_cycle({
                    "scenario_key": "s01", "closed_loop_cycle": cycle,
                })
                self.assertEqual(1, count)
                self.assertTrue(
                    (recorder.run_dir / "closed_loop_cycle.json").is_file()
                )
                row = recorder.store.connection.execute(
                    "SELECT scenario_key,status,revision_before,revision_after,"
                    "physical_execution,measurement_status,state_before_json,"
                    "next_state_json FROM closed_loop_cycles WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()
                migration = recorder.store.connection.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version='3'"
                ).fetchone()[0]
            finally:
                recorder.close()
        self.assertEqual("s01", row[0])
        self.assertEqual("SUCCEEDED", row[1])
        self.assertEqual((0, 2), (row[2], row[3]))
        self.assertEqual(0, row[4])
        self.assertEqual("STRUCTURAL_ONLY_NO_PHYSICS", row[5])
        self.assertEqual("pending", json.loads(row[6])["tasks"][0]["status"])
        self.assertEqual("completed", json.loads(row[7])["tasks"][0]["status"])
        self.assertEqual(1, migration)

    def test_structural_compound_road_fault_chain_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s09-compound-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "closed_edge_id": "edge-closed",
                "compound_events": [
                    {"event_type": "road_closure", "tick": 30},
                    {"event_type": "vehicle_failure", "tick": 40},
                ],
                "compound_event_parameters": {
                    "road_closure_tick": 30,
                    "vehicle_failure_tick": 40,
                    "recovery_tick": 80,
                },
                "failed_vehicle_id": "truck-1",
                "released_task_ids": ["task-2"],
                "route_changes": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "action_type": "route_replan_same_vehicle",
                    "candidate_evaluations": [],
                }],
                "compound_failure_decisions": [{
                    "task_id": "task-2", "failed_vehicle_id": "truck-1",
                    "selected_vehicle_id": "truck-2",
                    "action_type": "task_takeover_during_road_closure",
                    "policy_version": "compound-road-fault-safe-takeover-v1",
                    "constraint_results": {"closed_edge_avoided": True},
                    "candidate_evaluations": [],
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:initial", "task_id": "task-1",
                    "vehicle_id": "truck-1", "status": "planned",
                }, {
                    "route_plan_id": "task-2:initial", "task_id": "task-2",
                    "vehicle_id": "truck-1", "status": "planned",
                }, {
                    "route_plan_id": "task-1:replanned", "task_id": "task-1",
                    "vehicle_id": "truck-1", "status": "planned",
                }, {
                    "route_plan_id": "task-2:compound-fault-takeover",
                    "task_id": "task-2", "vehicle_id": "truck-2",
                    "status": "planned",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 80,
                }, {
                    "task_id": "task-2", "status": "completed",
                    "completed_tick": 80,
                }],
            }
            try:
                self.assertEqual(11, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                ordered_events = [row[0] for row in connection.execute(
                    "SELECT event_type FROM events WHERE run_id=? ORDER BY event_id",
                    (recorder.run_id,),
                ).fetchall()]
                self.assertEqual("road_closed", ordered_events[0])
                self.assertLess(
                    ordered_events.index("road_closed"),
                    ordered_events.index("vehicle_fault"),
                )
                self.assertLess(
                    ordered_events.index("vehicle_fault"),
                    ordered_events.index("task_reassigned"),
                )
                self.assertEqual("run_completed", ordered_events[-1])
                self.assertEqual(2, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertEqual(4, connection.execute(
                    "SELECT count(*) FROM route_plans WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_structural_blast_control_is_persisted_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s04-blast-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "restricted_edge_id": "edge-blast",
                "blast_event": {
                    "event_type": "planned_blasting_temporary_control",
                    "notice_tick": 20, "blast_start_tick": 40,
                    "clearance_tick": 60,
                    "exclusion_scope": "TOPOLOGY_EDGE_ANCHOR_ONLY",
                    "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                },
                "blast_decisions": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "original_vehicle_id": "truck-1",
                    "action_type": "hold_until_blast_clearance",
                    "policy_version": "planned-blast-road-control-v1",
                    "reason": "no_safe_bypass_wait_for_temporary_control_release",
                    "wait_until_tick": 60,
                    "measurement_status": "STRUCTURAL_ROUTE_LOGIC_NOT_CARLA_MEASURED",
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:replanned", "task_id": "task-1",
                    "vehicle_id": "truck-1", "status": "scheduled_after_blast_clearance",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 60,
                    "status_reason": "completed_after_blast_wait",
                }],
            }
            try:
                self.assertEqual(8, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? "
                    "GROUP BY event_type", (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["blast_announced"])
                self.assertEqual(1, events["blast_control_activated"])
                self.assertEqual(1, events["vehicle_held_for_blast"])
                self.assertEqual(1, events["blast_area_cleared"])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_structural_equipment_switch_is_persisted_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s03-equipment-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "affected_task_ids": ["task-1"],
                "equipment_event": {
                    "event_type": "loading_equipment_failure",
                    "equipment_id": "loader@point-a", "failure_tick": 30,
                    "recovery_tick": 70,
                    "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                },
                "equipment_decisions": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "action_type": "switch_to_alternative_work_point",
                    "policy_version": "equipment-failure-work-point-switch-v1",
                    "failed_equipment_id": "loader@point-a",
                    "alternative_equipment_id": "loader@point-b",
                    "failed_work_point_id": "point-a",
                    "alternative_work_point_id": "point-b",
                    "estimated_task_delay_s": 40.0,
                    "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
                    "constraint_results": {"alternative_route_reachable": True},
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:alternative-work-point",
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "status": "planned",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 70,
                    "status_reason": "completed_at_alternative_work_point",
                }],
            }
            try:
                self.assertEqual(7, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? "
                    "GROUP BY event_type", (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["equipment_fault"])
                self.assertEqual(1, events["task_paused"])
                self.assertEqual(1, events["task_work_point_switched"])
                self.assertEqual(1, events["equipment_recovered"])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_structural_congestion_control_is_persisted_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s06-traffic-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "bottleneck_edge_id": "edge-bottleneck",
                "congestion_event": {
                    "event_type": "shared_road_capacity_degradation",
                    "event_tick": 30, "recovery_tick": 60,
                    "road_capacity_vehicles": 1,
                    "minimum_safety_headway_seconds": 8.0,
                    "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                },
                "traffic_decisions": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "action_type": "hold_for_safe_headway",
                    "policy_version": "shared-road-capacity-scheduler-v1",
                    "estimated_arrival_s": 10.0, "scheduled_entry_s": 20.0,
                    "scheduled_exit_s": 30.0, "estimated_wait_s": 10.0,
                    "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:traffic-baseline",
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "status": "active",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 60,
                    "status_reason": "completed_after_traffic_control",
                }],
            }
            try:
                self.assertEqual(8, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? "
                    "GROUP BY event_type", (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["congestion_detected"])
                self.assertEqual(1, events["traffic_control_activated"])
                self.assertEqual(1, events["vehicle_held"])
                self.assertEqual(1, events["traffic_control_released"])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM route_plans WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_structural_weather_response_is_persisted_with_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s05-weather-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "degraded_edge_id": "edge-weather",
                "weather_event": {
                    "event_type": "extreme_rainfall_road_capacity_degradation",
                    "event_tick": 30, "recovery_tick": 60,
                    "restricted_speed_factor": 0.5,
                    "data_origin": "PARAMETERIZED_SYNTHETIC_SCENARIO",
                },
                "weather_decisions": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "action_type": "weather_speed_restriction",
                    "policy_version": "weather-aware-route-policy-v1",
                    "baseline_eta_s": 10.0, "selected_eta_s": 15.0,
                    "estimated_delay_s": 5.0,
                    "measurement_status": "SURROGATE_ONLY_NOT_CARLA_MEASURED",
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:weather-response",
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "status": "planned",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 60,
                    "status_reason": "completed_after_weather_response",
                }],
            }
            try:
                self.assertEqual(8, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? "
                    "GROUP BY event_type", (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["weather_started"])
                self.assertEqual(1, events["road_restricted"])
                self.assertEqual(1, events["weather_speed_restricted"])
                self.assertEqual(1, events["weather_recovered"])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_missing_policy_comparison_does_not_create_empty_decisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "no-policy-test"
            )
            try:
                self.assertEqual(0, recorder.record_policy_comparison({
                    "mode": "mock_structural",
                    "assignments": [{"task_id": "task-1", "vehicle_id": "truck-1"}],
                }))
                self.assertEqual(0, recorder.store.connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                recorder.close()

    def test_structural_road_replan_is_persisted_with_route_plans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s07-random-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "closed_edge_id": "edge-2",
                "route_planner_version": "RoadGraph-Dijkstra-Topology-V1",
                "route_changes": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "action_type": "route_replan_same_vehicle",
                    "original_distance_m": 100.0, "replanned_distance_m": 120.0,
                }],
                "route_plans": [{
                    "route_plan_id": "task-1:replanned", "task_id": "task-1",
                    "vehicle_id": "truck-1", "status": "planned",
                }],
                "tasks": [{
                    "task_id": "task-1", "status": "completed",
                    "completed_tick": 31, "status_reason": "completed_after_replan",
                }],
            }
            try:
                self.assertEqual(6, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? GROUP BY event_type",
                    (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["road_closed"])
                self.assertEqual(1, events["route_replanned"])
                self.assertEqual(1, events["road_reopened"])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM route_plans WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
            finally:
                connection.close()

    def test_structural_road_takeover_adds_task_reassignment_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s07-takeover-test"
            )
            result = {
                "status": "PASS", "mode": "mock_structural",
                "closed_edge_id": "edge-2", "route_plans": [], "tasks": [],
                "route_changes": [{
                    "task_id": "task-1", "original_vehicle_id": "truck-1",
                    "vehicle_id": "truck-2", "takeover_required": True,
                    "action_type": "task_takeover_after_no_safe_bypass",
                }],
            }
            try:
                recorder.record_structural_events(result)
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                transition = connection.execute(
                    "SELECT event_type,reason FROM task_transitions "
                    "WHERE run_id=? AND task_id='task-1'",
                    (recorder.run_id,),
                ).fetchone()
                self.assertEqual(
                    ("task_reassigned", "original_vehicle_has_no_safe_road_bypass"),
                    transition,
                )
            finally:
                connection.close()

    def test_structural_failure_events_are_persisted_from_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s02-random-test"
            )
            result = {
                "status": "PASS",
                "mode": "mock_structural",
                "failed_vehicle_id": "truck-2",
                "failure_tick": 20,
                "released_task_ids": ["task-2"],
                "assignments": [
                    {"task_id": "task-2", "vehicle_id": "truck-3", "score": 42.0}
                ],
                "tasks": [
                    {
                        "task_id": "task-2",
                        "status": "completed",
                        "completed_tick": 21,
                        "status_reason": "structural_mock_completion_after_takeover",
                    }
                ],
            }
            try:
                self.assertEqual(5, recorder.record_structural_events(result))
            finally:
                recorder.close()
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                events = dict(connection.execute(
                    "SELECT event_type,count(*) FROM events WHERE run_id=? "
                    "GROUP BY event_type",
                    (recorder.run_id,),
                ).fetchall())
                self.assertEqual(1, events["vehicle_fault"])
                self.assertEqual(1, events["task_released"])
                self.assertEqual(1, events["task_reassigned"])
                self.assertEqual(1, events["task_completed"])
                self.assertEqual(1, events["run_completed"])
            finally:
                connection.close()

    def test_shadow_multi_objective_costs_are_persisted_without_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = EvidenceRecorder(
                Path(temp_dir) / "artifacts" / "runs", "s01-random-test"
            )
            result = {
                "mode": "mock_structural",
                "assignments": [
                    {
                        "task_id": "task-1",
                        "zone_id": "task-1",
                        "vehicle_id": "truck-1",
                        "score": 850.0,
                        "reason": "p5_route_length",
                    }
                ],
                "candidate_rankings": {
                    "task-1": [
                        {
                            "task_id": "task-1",
                            "zone_id": "task-1",
                            "vehicle_id": "truck-1",
                            "score": 850.0,
                            "reason": "p5_route_length",
                        }
                    ]
                },
                "policy_comparison": {
                    "executed_policy": "heuristic-route-load-global-unique-v0",
                    "shadow_policy": "multi-objective-cost-v1",
                    "optimizer_version": "global-unique-multi-objective-v1",
                    "comparisons": [
                        {
                            "task_id": "task-1",
                            "executed_vehicle_v0": "truck-1",
                            "shadow_selected_vehicle_v1": "truck-2",
                            "selection_changed": True,
                            "v1_candidate_ranking": [
                                {
                                    "vehicle_id": "truck-2",
                                    "task_id": "task-1",
                                    "feasible": True,
                                    "constraint_results": [
                                        {"constraint": "road_open", "passed": True}
                                    ],
                                    "cost_time": {
                                        "value": None,
                                        "normalized": None,
                                        "availability": "surrogate_only",
                                    },
                                    "cost_transport": {
                                        "value": 900.0,
                                        "normalized": 0.0,
                                        "availability": "available",
                                    },
                                    "total_cost": 0.0,
                                    "policy_version": "multi-objective-cost-v1",
                                    "selected": True,
                                }
                            ],
                        }
                    ],
                },
            }
            try:
                self.assertEqual(2, recorder.record_policy_comparison(result))
                recorder.write_json(
                    "summary.json",
                    {
                        "status": "PASS",
                        "scenario_seed": 202601,
                        "scenario_mode": "seeded_random_map",
                        "mode": "mock_structural",
                        "policy_version": "heuristic-route-load-global-unique-v0",
                    },
                )
            finally:
                recorder.close()

            connection = sqlite3.connect(str(recorder.database_path))
            try:
                rows = connection.execute(
                    "SELECT context, vehicle_id, policy_version FROM decisions "
                    "WHERE run_id=? ORDER BY context",
                    (recorder.run_id,),
                ).fetchall()
                self.assertEqual(2, len(rows))
                self.assertIn(
                    (
                        "structural_shadow_policy_comparison",
                        "truck-2",
                        "multi-objective-cost-v1",
                    ),
                    rows,
                )
                payload = connection.execute(
                    "SELECT payload_json FROM decision_candidates "
                    "WHERE run_id=? AND decision_key=? AND vehicle_id=?",
                    (recorder.run_id, "task-1:shadow-multi-objective", "truck-2"),
                ).fetchone()[0]
                candidate = json.loads(payload)
                self.assertEqual(900.0, candidate["cost_transport"]["value"])
                self.assertTrue(candidate["constraint_results"][0]["passed"])
                self.assertEqual(
                    "heuristic-route-load-global-unique-v0",
                    connection.execute(
                        "SELECT policy_version FROM scenario_runs WHERE run_id=?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_relative_legacy_artifacts_path_never_targets_system_data(self):
        original_cwd = Path.cwd()
        try:
            os.chdir("/")
            path = EvidenceRecorder._default_database_path(
                Path("artifacts/runs")
            )
        finally:
            os.chdir(str(original_cwd))

        self.assertNotEqual(Path("/data/database/openpit.db"), path)
        self.assertEqual("openpit.db", path.name)

    def test_recorder_preserves_json_evidence_and_indexes_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts" / "runs"
            recorder = EvidenceRecorder(artifacts_root, "scenario-test")
            recorder.record(
                "risk_assessed",
                {
                    "assessment_id": "assessment-1",
                    "tick": 12,
                    "zone_id": "slope-zone",
                    "level": "red",
                    "trend": "rising",
                    "reasons": ["synthetic_threshold"],
                    "metrics": {"rainfall": 50.0},
                },
            )
            recorder.record(
                "agent_decision",
                {
                    "decision_id": "decision-1",
                    "tick": 13,
                    "context": "hazard_takeover",
                    "task_id": "task-1",
                    "scheduler_agent_action": {
                        "assigned_vehicle_id": "truck-2",
                        "score": 10.5,
                        "policy_version": "rule-v0",
                    },
                    "candidate_evaluations": [
                        {
                            "vehicle_id": "truck-2",
                            "score": 10.5,
                            "reason": "capability_and_distance",
                        },
                        {
                            "vehicle_id": "truck-3",
                            "score": 12.0,
                            "reason": "farther_distance",
                        },
                    ],
                },
            )
            recorder.write_jsonl(
                "monitoring_observations.jsonl", [{"sample_id": "s1"}]
            )
            recorder.write_json(
                "summary.json",
                {
                    "status": "PASS",
                    "scenario_seed": 202616,
                    "scenario_mode": "fixed",
                    "mode": "mock",
                    "result": {"completion_rate": 1.0},
                    "tasks": [
                        {
                            "task_id": "task-1",
                            "zone_id": "zone-1",
                            "task_type": "inspection",
                            "priority": 1,
                            "status": "completed",
                            "assigned_vehicle_id": "truck-2",
                            "completed_tick": 99,
                        }
                    ],
                },
            )
            recorder.close()

            self.assertTrue((recorder.run_dir / "events.jsonl").is_file())
            self.assertTrue(recorder.database_path.is_file())
            connection = sqlite3.connect(str(recorder.database_path))
            try:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM scenario_runs WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "red",
                    connection.execute(
                        "SELECT level FROM risk_assessments WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "truck-2",
                    connection.execute(
                        "SELECT vehicle_id FROM decisions WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) FROM decision_candidates WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        """
                        SELECT selected FROM decision_candidates
                        WHERE run_id = ? AND vehicle_id = 'truck-2'
                        """,
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "completed",
                    connection.execute(
                        "SELECT status FROM tasks WHERE run_id = ?",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1.0,
                    connection.execute(
                        "SELECT metric_value FROM metrics WHERE run_id = ? "
                        "AND metric_name = 'completion_rate'",
                        (recorder.run_id,),
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_episode_metadata_and_route_plan_are_queryable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "openpit.db"
            config = load_config(
                PROJECT_ROOT / "configs" / "mine_competition_demo.json"
            )
            episode = build_episode(config, run_id="episode-db-v2-001")
            store = SqliteRunStore(database_path)
            store.record_episode(
                episode,
                config_path=PROJECT_ROOT / "configs" / "mine_competition_demo.json",
                policy_version="RulePolicy-V0",
                route_planner_version="BasicRoute-V0",
                risk_model_version="RuleRisk-V0",
            )
            store.record_route_plan(
                episode.run_id,
                {
                    "route_plan_id": "route-v2-001",
                    "vehicle_id": "inspection_vehicle_01",
                    "task_id": "initial:routine_zone_01",
                    "planner_version": "BasicRoute-V0",
                    "distance_m": 120.0,
                    "status": "planned",
                },
            )
            run = store.connection.execute(
                """
                SELECT fleet_size, available_fleet_size, task_load, policy_version
                FROM scenario_runs WHERE run_id = ?
                """,
                (episode.run_id,),
            ).fetchone()
            self.assertEqual((3, 3, "legacy", "RulePolicy-V0"), run)
            self.assertEqual(
                3,
                store.connection.execute(
                    "SELECT COUNT(*) FROM run_vehicles WHERE run_id = ?",
                    (episode.run_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                store.connection.execute(
                    "SELECT COUNT(*) FROM episode_tasks WHERE run_id = ?",
                    (episode.run_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                store.connection.execute(
                    "SELECT COUNT(*) FROM episode_events WHERE run_id = ?",
                    (episode.run_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                store.connection.execute(
                    "SELECT COUNT(*) FROM route_plans WHERE run_id = ?",
                    (episode.run_id,),
                ).fetchone()[0],
            )
            store.close()

    def test_learning_status_and_shadow_policy_registry_are_separate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "openpit.db"
            store = SqliteRunStore(database_path)
            try:
                for scenario_key in ("s01", "s02", "s04", "s09"):
                    run_id = "{}-6v-seed-101-run".format(scenario_key)
                    store.start_run(run_id, "{}-6v-seed-101".format(scenario_key))
                    store.connection.execute(
                        "UPDATE scenario_runs SET status='PASS', "
                        "scenario_seed=101, simulator_mode="
                        "'carla_multi_scenario_execution' WHERE run_id=?",
                        (run_id,),
                    )
                    store.connection.execute(
                        "INSERT INTO events(run_id,timestamp,tick,event_type,payload_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (run_id, "2026-01-01T00:00:00+00:00", 1,
                         "decision_experience_captured", "{}"),
                    )
                store.connection.commit()
                collecting = store.learning_status_report(5)
                self.assertEqual("COLLECTING_DATA", collecting["status"])
                self.assertEqual(4, collecting["valid_carla_run_count"])
                self.assertEqual(4, collecting["decision_experience_count"])
                ready = store.learning_status_report(4)
                self.assertEqual("UPDATE_CHECK_DUE", ready["status"])
                store.register_policy_version(
                    "bc-test-v1", "behavior-cloning-candidate-ranker-v1",
                    "OFFLINE_EVALUATED_SHADOW_ONLY", "shadow_only",
                    dataset_version="dataset-test-v1",
                    model_path="models/bc-test-v1/model.json",
                )
                store.record_training_run(
                    "training-test-v1", "bc-test-v1", "dataset-test-v1",
                    "candidate", "TRAINED_OFFLINE_SHADOW_ONLY",
                    "models/bc-test-v1/training_report.json", {},
                )
                store.record_policy_evaluation(
                    "evaluation-test-v1", "bc-test-v1", "offline_holdout",
                    "OFFLINE_EVALUATED_SHADOW_ONLY", {"record_count": 10},
                )
                after_training = store.learning_status_report(4)
                self.assertEqual("COLLECTING_DATA", after_training["status"])
                self.assertEqual(0, after_training["valid_carla_run_count"])
                self.assertEqual(
                    "multi-objective-cost-v1",
                    after_training["formal_policy"]["model_version"],
                )
                self.assertEqual(
                    "bc-test-v1",
                    after_training["candidate_policy"]["model_version"],
                )
                self.assertEqual(
                    "shadow_only",
                    after_training["candidate_policy"]["execution_authority"],
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
