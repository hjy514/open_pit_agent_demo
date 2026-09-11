import json
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from open_pit_agent.config import load_config
from open_pit_agent.cli import _normalize_s08_golden_summary
from open_pit_agent.decision_intelligence import (
    aggregate_structural_transition_datasets, build_structural_transition_dataset,
    load_structural_reward_config, write_structural_transition_dataset,
    export_closed_loop_transition_dataset,
    load_decision_experience_reward_config,
    export_decision_experience_transition_dataset,
)
from open_pit_agent.scenario import (
    SCENARIO_CATALOG, build_structural_runtime_snapshots,
    normalize_scenario_run_result, scenario_spec,
    run_structural_scenario, summarize_structural_batch,
    validate_structural_closed_loop,
)
from open_pit_agent.scenario.carla_execution import (
    _event_is_ready, _fleet_launch_schedule, _production_cycle_plans,
    _recently_progressing_task_ids, _reconcile_route_admission,
    _reconcile_runtime_traffic, _runtime_traffic_summary,
    _retry_waiting_recovery_tasks,
    _recommended_execution_ticks,
    _task_execution_diagnostics, _update_progress_watchdog,
    run_carla_scenario_execution,
    run_s01_carla_execution,
)
from open_pit_agent.scenario.generator import sample_event_timing
from open_pit_agent.evidence import EvidenceRecorder
from open_pit_agent.runtime_state import RuntimeState
from open_pit_agent.closed_loop import ClosedLoopCoordinator
from open_pit_agent.map_resources import MapResourceStore


def planner_pairs_for_fake_carla(config):
    binding = config.map_resource
    with MapResourceStore(binding.database_path) as store:
        return {
            (str(item["from_point_id"]), str(item["to_point_id"]))
            for item in store.planner_reachable_pairs(
                binding.map_id, binding.resource_version
            )
        }


def physical_pairs_for_fake_carla(config):
    binding = config.map_resource
    with MapResourceStore(binding.database_path) as store:
        return {
            (str(item["from_point_id"]), str(item["to_point_id"]))
            for item in store.physical_route_validations(
                binding.map_id, binding.resource_version
            )
            if item.get("validation_status") == "PHYSICAL_REACHED"
        }


class RecordingScenarioAdapter:
    """Fast contract adapter used to exercise every CARLA scenario branch."""

    last_instance = None

    def __init__(self, config, load_map=False):
        self.config = config
        self.load_map = load_map
        self.tasks = []
        self.tick_count = 0
        self.operations = []
        self.paused = set()
        self.faulted = set()
        RecordingScenarioAdapter.last_instance = self

    def connect(self):
        self.operations.append(("connect", None))

    def ensure_vehicles(self, spawn_missing):
        self.operations.append(("ensure_vehicles", bool(spawn_missing)))

    def resolve_zones(self, zones):
        return zones

    def dispatch(self, tasks, zones):
        self.tasks = list(tasks)
        for task in self.tasks:
            task.status = "executing"
            task.last_distance_m = 1200.0
        self.operations.append(("dispatch", len(self.tasks)))

    def tick(self):
        self.tick_count += 1
        for task in self.tasks:
            if task.status in {"completed", "timed_out", "cancelled", "stuck"}:
                continue
            if task.assigned_vehicle_id in self.paused:
                continue
            task.last_distance_m = max(
                0.0, float(task.last_distance_m or 1200.0) - 1.0
            )
            if self.tick_count >= 1100:
                task.status = "completed"
                task.status_reason = "recording_adapter_completed"
        return {}

    def inject_fault(self, vehicle_id):
        self.faulted.add(str(vehicle_id))
        self.operations.append(("inject_fault", str(vehicle_id)))
        return {"vehicle_id": str(vehicle_id), "status": "fault"}

    def retire_vehicle(self, vehicle_id, reason):
        self.operations.append(("retire_vehicle", str(vehicle_id)))
        return {
            "vehicle_id": str(vehicle_id),
            "status": "RETIRED_FROM_EPISODE",
            "reason": str(reason),
            "parking_resource_status": "NOT_AVAILABLE",
        }

    def reassign_task(self, task_id, vehicle_id, speed_limit_kmh=None,
                      assignment_source="manual_dispatch"):
        task = next(item for item in self.tasks if item.task_id == task_id)
        task.assigned_vehicle_id = str(vehicle_id)
        task.status = "executing"
        self.operations.append(("reassign_task", str(task_id)))
        return {"task_id": str(task_id), "vehicle_id": str(vehicle_id),
                "status": "executing"}

    def retarget_task(self, task_id, zone, speed_limit_kmh=None):
        self.operations.append(("retarget_task", str(task_id)))
        return {"task_id": str(task_id), "vehicle_id": None,
                "status": "executing"}

    def set_task_speed_limit(self, task_id, speed_limit_kmh):
        self.operations.append(("set_task_speed_limit", str(task_id)))
        return {"task_id": str(task_id),
                "speed_limit_kmh": float(speed_limit_kmh)}

    def set_task_route(self, task_id, vehicle_id, waypoint_positions,
                       blocked_edge_id=None, route_edge_ids=None):
        self.operations.append(("set_task_route", str(task_id)))
        return {
            "task_id": str(task_id), "vehicle_id": str(vehicle_id),
            "waypoint_count": len(waypoint_positions), "status": "executing",
        }

    def configure_deferred_task_route(
        self, task_id, vehicle_id, waypoint_positions,
        blocked_edge_id=None, route_edge_ids=None,
    ):
        self.operations.append(("configure_deferred_task_route", str(task_id)))
        return {
            "task_id": str(task_id), "vehicle_id": str(vehicle_id),
            "waypoint_count": len(waypoint_positions),
            "status": "DEFERRED_UNTIL_TASK_RESUME",
        }

    def pause_vehicle(self, vehicle_id):
        self.paused.add(str(vehicle_id))
        self.operations.append(("pause_vehicle", str(vehicle_id)))
        return {"vehicle_id": str(vehicle_id), "status": "paused"}

    def resume_vehicle(self, vehicle_id):
        self.paused.discard(str(vehicle_id))
        self.operations.append(("resume_vehicle", str(vehicle_id)))
        return {"vehicle_id": str(vehicle_id), "status": "executing"}

    def drain_events(self):
        return []

    def list_states(self):
        return [SimpleNamespace(
            vehicle_id=item.vehicle_id, role_name=item.role_name,
            health="fault" if item.vehicle_id in self.faulted else "healthy",
            available=item.vehicle_id not in self.faulted,
            task_status=("paused" if item.vehicle_id in self.paused else "executing"),
            current_task_id=None, position=None, speed_mps=0.0,
        ) for item in self.config.vehicles]

    def destroy_spawned_vehicles(self):
        self.operations.append(("destroy_spawned_vehicles", None))
        return len(self.config.vehicles)

    def close(self):
        self.operations.append(("close", None))


class UnifiedScenarioRunnerTests(unittest.TestCase):
    def test_progress_watchdog_resets_when_navigation_leg_changes(self):
        task = SimpleNamespace(
            task_id="task-cycle", assigned_vehicle_id="truck-1",
            status="executing", status_reason="returning",
            last_distance_m=200.0, completed_tick=None,
        )
        progress = {
            "task-cycle": {
                "initial_distance_m": 100.0,
                "best_distance_m": 5.0,
                "last_progress_tick": 100,
                "progress_m": 95.0,
                "restart_count": 1,
                "progress_phase": ("truck-1", "loaded_haul"),
            }
        }

        controls = _update_progress_watchdog(
            [task], 1000, SimpleNamespace(), progress, {}, []
        )

        self.assertEqual([], controls)
        self.assertEqual("executing", task.status)
        self.assertEqual(
            200.0, progress["task-cycle"]["initial_distance_m"]
        )
        self.assertEqual(0, progress["task-cycle"]["restart_count"])
        self.assertEqual(
            ("truck-1", "returning"),
            progress["task-cycle"]["progress_phase"],
        )

    def test_progress_watchdog_accepts_motion_on_a_route_curving_away(self):
        task = SimpleNamespace(
            task_id="task-curve", assigned_vehicle_id="truck-1",
            status="executing", status_reason="returning",
            last_distance_m=120.0, completed_tick=None,
        )
        progress = {
            "task-curve": {
                "initial_distance_m": 100.0,
                "best_distance_m": 100.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 1,
                "progress_phase": ("truck-1", "returning"),
            },
        }
        controls = _update_progress_watchdog(
            [task], 700, SimpleNamespace(), progress, {}, [],
            {"truck-1": 4.1},
        )
        self.assertEqual([], controls)
        self.assertEqual("executing", task.status)
        self.assertEqual(700, progress["task-curve"]["last_progress_tick"])
        self.assertTrue(progress["task-curve"]["motion_observed"])

    def test_progress_watchdog_waits_for_measured_traffic_blockage(self):
        task = SimpleNamespace(
            task_id="traffic-wait", assigned_vehicle_id="truck-1",
            status="executing", status_reason="navigation_started",
            last_distance_m=100.0, completed_tick=None,
        )

        class TrafficBlockedAdapter:
            def __init__(self):
                self.events = []

            def classify_navigation_blockage(self, vehicle_id):
                return {
                    "category": "TRAFFIC_BLOCKED",
                    "vehicle_id": vehicle_id,
                    "blocker_vehicle_id": "truck-2",
                    "surface_gap_m": 6.0,
                }

            def set_task_speed_limit(self, task_id, speed_limit_kmh):
                raise AssertionError("traffic wait must not restart navigation")

            def _emit(self, event_type, payload):
                self.events.append((event_type, payload))

        progress = {
            "traffic-wait": {
                "initial_distance_m": 100.0,
                "best_distance_m": 100.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 0,
                "progress_phase": ("truck-1", "navigation_started"),
            },
        }
        adapter = TrafficBlockedAdapter()

        controls = _update_progress_watchdog(
            [task], 700, adapter, progress, {}, [], {"truck-1": 0.0}, {}
        )

        self.assertEqual("executing", task.status)
        self.assertEqual("TRAFFIC_WAIT", controls[0]["status"])
        self.assertEqual(
            "wait_for_detected_vehicle_blockage", controls[0]["action"]
        )
        self.assertEqual(700, progress["traffic-wait"]["last_progress_tick"])
        self.assertEqual("truck-2", progress["traffic-wait"]["blocker_vehicle_id"])
        self.assertEqual(
            "vehicle_navigation_waiting_for_traffic", adapter.events[0][0]
        )

    def test_progress_watchdog_escalates_persistent_traffic_wait(self):
        task = SimpleNamespace(
            task_id="persistent-traffic", assigned_vehicle_id="truck-1",
            status="executing", status_reason="navigation_started",
            last_distance_m=100.0,
            completed_tick=None,
        )

        class PersistentBlockAdapter:
            def __init__(self):
                self.restarts = []

            def classify_navigation_blockage(self, vehicle_id):
                return {
                    "category": "TRAFFIC_BLOCKED",
                    "vehicle_id": vehicle_id,
                    "blocker_vehicle_id": "truck-2",
                }

            def set_task_speed_limit(self, task_id, speed_limit_kmh):
                self.restarts.append(task_id)
                return {
                    "task_id": task_id,
                    "speed_limit_kmh": speed_limit_kmh,
                }

            def _emit(self, event_type, payload):
                return None

        progress = {
            "persistent-traffic": {
                "initial_distance_m": 100.0,
                "best_distance_m": 100.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 0,
                "progress_phase": ("truck-1", "navigation_started"),
                "traffic_wait_started_tick": 100,
            },
        }
        adapter = PersistentBlockAdapter()

        controls = _update_progress_watchdog(
            [task], 1300, adapter, progress, {}, [], {"truck-1": 0.0}, {}
        )

        self.assertEqual(["persistent-traffic"], adapter.restarts)
        self.assertEqual(
            ["escalate_persistent_vehicle_blockage",
             "restart_stalled_navigation"],
            [item["action"] for item in controls],
        )

    def test_progress_watchdog_ignores_preempted_queued_task(self):
        task = SimpleNamespace(
            task_id="queued-task", assigned_vehicle_id="truck-1",
            status="assigned", status_reason="preempted_by_human_dispatch",
            last_distance_m=80.0, completed_tick=None,
        )
        progress = {
            "queued-task": {
                "initial_distance_m": 100.0,
                "best_distance_m": 80.0,
                "last_progress_tick": 0,
                "progress_m": 20.0,
                "restart_count": 1,
                "progress_phase": ("truck-1", "executing"),
            },
        }

        controls = _update_progress_watchdog(
            [task], 1000, SimpleNamespace(), progress, {}, [],
            {"truck-1": 0.0},
        )

        self.assertEqual([], controls)
        self.assertEqual("assigned", task.status)
        self.assertNotIn("queued-task", progress)

    def test_progress_watchdog_recognises_live_tasks_before_global_limit(self):
        tasks = [
            SimpleNamespace(task_id="moving", status="executing"),
            SimpleNamespace(task_id="old", status="executing"),
            SimpleNamespace(task_id="done", status="completed"),
        ]
        progress = {
            "moving": {
                "initial_distance_m": 100.0,
                "progress_m": 20.0, "last_progress_tick": 900,
            },
            "old": {
                "initial_distance_m": 100.0,
                "progress_m": 20.0, "last_progress_tick": 100,
            },
            "done": {
                "initial_distance_m": 100.0,
                "progress_m": 20.0, "last_progress_tick": 999,
            },
        }
        self.assertEqual(
            ["moving"],
            _recently_progressing_task_ids(tasks, 1000, progress),
        )

    def test_progress_watchdog_reassigns_stalled_work_once(self):
        stalled = SimpleNamespace(
            task_id="stalled-task", assigned_vehicle_id="truck-1",
            status="executing", status_reason="navigation_started",
            last_distance_m=100.0, completed_tick=None, priority=50,
            required_capabilities=["haul"], original_vehicle_id=None,
            handover_reason=None, handover_tick=None,
        )
        existing = SimpleNamespace(
            task_id="existing-task", assigned_vehicle_id="truck-2",
            status="executing", priority=40, last_distance_m=None,
        )

        class RecoveryAdapter:
            def __init__(self):
                self.retired = []
                self.reassigned = []
                self.events = []

            def list_states(self):
                return [
                    SimpleNamespace(
                        vehicle_id="truck-2", capabilities=["haul"],
                        available=True, health="healthy",
                        task_status="executing",
                    ),
                    SimpleNamespace(
                        vehicle_id="truck-3", capabilities=["haul"],
                        available=True, health="healthy",
                        task_status="idle",
                    ),
                ]

            def retire_vehicle(self, vehicle_id, reason):
                self.retired.append((vehicle_id, reason))
                return {"vehicle_id": vehicle_id, "status": "RETIRED_FROM_EPISODE"}

            def reassign_task(self, task_id, vehicle_id,
                              assignment_source="manual_dispatch"):
                stalled.assigned_vehicle_id = vehicle_id
                stalled.status = "executing"
                stalled.status_reason = "assigned_by_{}".format(
                    assignment_source
                )
                self.reassigned.append((task_id, vehicle_id, assignment_source))
                return {"task_id": task_id, "vehicle_id": vehicle_id,
                        "status": "executing"}

            def _emit(self, event_type, payload):
                self.events.append((event_type, payload))

        adapter = RecoveryAdapter()
        progress = {
            "stalled-task": {
                "initial_distance_m": 100.0,
                "best_distance_m": 100.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 1,
                "progress_phase": ("truck-1", "navigation_started"),
            },
        }
        recovery_attempts = {}

        controls = _update_progress_watchdog(
            [stalled, existing], 700, adapter, progress, {}, [],
            {"truck-1": 0.0}, recovery_attempts,
        )

        self.assertEqual("executing", stalled.status)
        self.assertEqual("truck-3", stalled.assigned_vehicle_id)
        self.assertEqual({"stalled-task": 1}, recovery_attempts)
        self.assertEqual(
            [("stalled-task", "truck-3", "scenario_recovery")],
            adapter.reassigned,
        )
        self.assertIn(
            "reassign_after_runtime_navigation_stall",
            [item["action"] for item in controls],
        )
        self.assertNotIn("stalled-task", progress)

    def test_exhausted_recovery_detaches_only_stalled_preemption(self):
        stalled = SimpleNamespace(
            task_id="stalled-task", assigned_vehicle_id="truck-2",
            status="executing", status_reason="navigation_started",
            last_distance_m=100.0, completed_tick=None, priority=50,
            required_capabilities=["haul"], original_vehicle_id="truck-1",
            handover_reason=None, handover_tick=None,
        )
        queued = SimpleNamespace(
            task_id="queued-task", assigned_vehicle_id="truck-2",
            status="assigned", priority=40, last_distance_m=80.0,
        )

        class QueueOwnerAdapter:
            def __init__(self):
                self.detached = []
                self.retired = []

            def detach_terminal_task_and_resume_vehicle(self, task_id,
                                                        reason):
                self.detached.append((task_id, reason))
                return {
                    "task_id": task_id, "vehicle_id": "truck-2",
                    "status": "TERMINAL_TASK_DETACHED",
                    "next_task_id": "queued-task", "vehicle_retained": True,
                }

            def retire_vehicle(self, vehicle_id, reason):
                self.retired.append((vehicle_id, reason))
                return {"vehicle_id": vehicle_id, "status": "RETIRED"}

        adapter = QueueOwnerAdapter()
        progress = {
            "stalled-task": {
                "initial_distance_m": 100.0,
                "best_distance_m": 100.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 1,
                "progress_phase": ("truck-2", "navigation_started"),
            },
            "__navigation_recovery_attempts__": {"stalled-task": 1},
        }
        controls = _update_progress_watchdog(
            [stalled, queued], 700, adapter, progress, {}, [],
            {"truck-2": 0.0}, {"stalled-task": 1},
        )

        self.assertEqual("stuck", stalled.status)
        self.assertEqual("assigned", queued.status)
        self.assertEqual([], adapter.retired)
        self.assertEqual(
            [("stalled-task",
              "terminal_stalled_task_releases_vehicle_queue")],
            adapter.detached,
        )
        self.assertEqual(
            "TERMINAL_TASK_DETACHED",
            controls[-1]["vehicle_clearance"]["status"],
        )

    def test_progress_watchdog_uses_forward_lane_recovery_before_reassignment(self):
        task = SimpleNamespace(
            task_id="route-stall", assigned_vehicle_id="truck-1",
            status="executing", status_reason="navigation_started",
            last_distance_m=80.0, completed_tick=None, priority=50,
            required_capabilities=["haul"],
        )

        class ManoeuvreAdapter:
            def __init__(self):
                self.recovery_calls = 0

            def recover_task_navigation(self, task_id):
                self.recovery_calls += 1
                task.status_reason = "navigation_recovery_waypoint_started"
                return {"task_id": task_id, "vehicle_id": "truck-1",
                        "status": "APPLIED"}

            def set_task_speed_limit(self, task_id, speed_limit_kmh):
                return {"task_id": task_id,
                        "speed_limit_kmh": speed_limit_kmh}

        adapter = ManoeuvreAdapter()
        progress = {
            "route-stall": {
                "initial_distance_m": 80.0,
                "best_distance_m": 80.0,
                "last_progress_tick": 0,
                "progress_m": 0.0,
                "restart_count": 1,
                "progress_phase": ("truck-1", "navigation_started"),
            },
        }

        controls = _update_progress_watchdog(
            [task], 700, adapter, progress, {}, [], {"truck-1": 0.0}, {}
        )

        self.assertEqual(1, adapter.recovery_calls)
        self.assertEqual("executing", task.status)
        self.assertEqual(
            1,
            progress["__navigation_recovery_attempts__"]["route-stall"],
        )
        self.assertEqual(
            "start_forward_lane_navigation_recovery", controls[0]["action"]
        )

    def test_carla_production_keeps_task_service_origin_after_reassignment(self):
        workload = {
            "tasks": [SimpleNamespace(
                task_id="task-1", task_type="haul_transport",
            )],
            "task_drafts": [{
                "task_id": "task-1", "vehicle_id": "truck-original",
                "from_point_id": "p1", "to_point_id": "p2",
                "validation_status": "PLANNER_REACHABLE",
            }],
            "vehicles": [
                SimpleNamespace(
                    vehicle_id="truck-original", spawn_point_index=11,
                ),
                SimpleNamespace(
                    vehicle_id="truck-selected", spawn_point_index=22,
                ),
            ],
            "vehicle_origins": {
                "truck-original": "p1", "truck-selected": "p3",
            },
        }
        plans = _production_cycle_plans(
            workload, {("p1", "p2")}, {("p2", "p1")},
            assignments={"task-1": "truck-selected"},
        )
        self.assertEqual("truck-selected", plans[0]["vehicle_id"])
        self.assertEqual(22, plans[0]["origin_spawn_point_index"])
        self.assertEqual("p1", plans[0]["origin_point_id"])
        self.assertEqual(
            "P5_PLANNER_REACHABLE_PENDING_PHYSICAL_VALIDATION",
            plans[0]["return_route_evidence"],
        )
        self.assertTrue(plans[0]["completion_after_dumping"])
        self.assertEqual(
            "SEPARATE_TASK_NOT_EXECUTED", plans[0]["return_execution"]
        )

    def test_common_production_plans_exclude_inspection_and_support_tasks(self):
        workload = {
            "tasks": [
                SimpleNamespace(task_id="haul", task_type="haul_transport"),
                SimpleNamespace(task_id="inspect", task_type="slope_inspection"),
                SimpleNamespace(task_id="support", task_type="equipment_support"),
            ],
            "task_drafts": [
                {"task_id": task_id, "vehicle_id": task_id + "-vehicle",
                 "from_point_id": task_id + "-from",
                 "to_point_id": task_id + "-to"}
                for task_id in ("haul", "inspect", "support")
            ],
            "vehicles": [
                SimpleNamespace(
                    vehicle_id=task_id + "-vehicle", spawn_point_index=index,
                )
                for index, task_id in enumerate(("haul", "inspect", "support"))
            ],
            "vehicle_origins": {
                task_id + "-vehicle": task_id + "-from"
                for task_id in ("haul", "inspect", "support")
            },
        }

        plans = _production_cycle_plans(workload, set(), set())

        self.assertEqual(["haul"], [item["task_id"] for item in plans])

    def test_v2_episode_contract_maps_all_nine_scenarios_without_fake_resources(self):
        required = {
            "schema_version", "episode_id", "scenario_key",
            "scenario_family", "complexity_profile", "map_context",
            "fleet", "production_system", "initial_state",
            "randomization", "event_plan", "hard_constraints",
            "admission", "acceptance", "execution_binding",
            "data_provenance", "implementation_status",
        }
        expected_events = {
            key: list(entry["events"])
            for key, entry in SCENARIO_CATALOG.items()
        }
        for key in sorted(SCENARIO_CATALOG):
            spec = scenario_spec(key, SCENARIO_CATALOG).to_dict()
            result = normalize_scenario_run_result({
                "scenario_id": "{}-contract-check".format(key),
                "seed": 202601,
                "status": "READY",
                "vehicle_count": spec["fleet"]["default_vehicle_count"],
                "task_count": 0,
                "completed_task_count": 0,
                "simulation_claim": "contract_check_only",
            }, key, map_context={
                "map_id": "0325_5", "resource_version": "1.0-draft",
            }, scenario_spec=spec)
            episode = result["concrete_episode_v2"]
            self.assertTrue(required.issubset(episode), key)
            self.assertEqual("openpit.concrete-episode.v2", episode["schema_version"])
            self.assertEqual(key, episode["scenario_key"])
            self.assertEqual([], episode["production_system"]["loaders"])
            self.assertEqual([], episode["production_system"]["dump_points"])
            self.assertEqual(
                "NOT_AVAILABLE_IN_BASELINE",
                episode["production_system"]["resource_data_status"],
            )
            self.assertEqual(
                expected_events[key],
                [item["event_type"] for item in episode["event_plan"]],
            )
            self.assertTrue(all(
                item["trigger"]["type"] == "state_condition"
                for item in episode["event_plan"]
            ))

    def test_capacity_hold_timeout_is_not_reported_as_navigation_stuck(self):
        task = SimpleNamespace(
            task_id="task-held", assigned_vehicle_id="truck-held",
            status="timed_out",
            status_reason="route_capacity_hold_at_safety_watchdog_limit",
            attempt_count=1, last_distance_m=200.0, started_tick=None,
            completed_tick=1000,
        )
        diagnostic = _task_execution_diagnostics([task], [])[0]
        self.assertEqual("CAPACITY_HOLD_TIMEOUT", diagnostic["failure_category"])

    def test_execution_diagnostics_do_not_invent_a_stall_root_cause(self):
        task = SimpleNamespace(
            task_id="task-stuck", assigned_vehicle_id="haul-01", status="stuck",
            status_reason="no_measurable_progress_after_navigation_restart",
            attempt_count=2, last_distance_m=123.0, started_tick=10,
            completed_tick=800,
        )
        diagnostics = _task_execution_diagnostics([task], [{
            "action": "restart_stalled_navigation", "task_id": "task-stuck",
        }])
        self.assertEqual(
            "NAVIGATION_NO_PROGRESS_AFTER_RESTART",
            diagnostics[0]["failure_category"],
        )
        self.assertEqual("NOT_IDENTIFIED", diagnostics[0]["root_cause"])
        self.assertTrue(diagnostics[0]["requires_follow_up"])

    def test_event_readiness_requires_launch_settle_and_measured_progress(self):
        task = SimpleNamespace(
            task_id="task-1", assigned_vehicle_id="inspection-1",
            status="executing",
        )
        schedule = [{"launch_tick": 0}, {"launch_tick": 480}]
        progress = {
            "__failed_vehicle__": {"vehicle_id": "inspection-1"},
            "task-1": {"initial_distance_m": 100.0, "progress_m": 0.0},
        }
        self.assertFalse(_event_is_ready(
            "s02", 30, 600, schedule, [task], progress, 4000
        ))
        progress["task-1"]["progress_m"] = 12.0
        self.assertTrue(_event_is_ready(
            "s02", 30, 600, schedule, [task], progress, 4000
        ))
    def test_structural_runtime_playback_uses_factual_map_points(self):
        result = {
            "scenario_key": "s02", "scenario_id": "s02-test",
            "seed": 7, "run_id": "run-7",
            "generated_episode": {
                "fleet": {"vehicles": [{
                    "vehicle_id": "truck-1", "role": "inspection",
                    "health": "healthy",
                }]},
                "tasks": [{
                    "task_id": "task-1", "vehicle_id": "truck-1",
                    "from_point_id": "p1", "to_point_id": "p2",
                }],
            },
            "initial_task_states": [{
                "task_id": "task-1", "assigned_vehicle_id": "truck-1",
                "status": "assigned",
            }],
            "tasks": [{
                "task_id": "task-1", "assigned_vehicle_id": "truck-1",
                "status": "completed",
            }],
            "failed_vehicle_id": "truck-1",
            "scenario_events": [{
                "event_id": "event-1", "event_type": "vehicle_failure",
                "payload": {"vehicle_id": "truck-1"},
            }],
            "decisions": [{"decision_id": "decision-1"}],
            "assignments": [],
            "provenance": {"map_context": {"map_id": "0325_5"}},
            "world_state": {"roads": {}, "traffic": {}, "equipment": {}},
            "outcome": {"status": "PASS"},
            "lifecycle": {"phases": []},
            "closed_loop_validation": {"status": "CLOSED_LOOP_PASS"},
        }
        snapshots = build_structural_runtime_snapshots(result, {
            "p1": {"x": 1, "y": 2, "z": 3, "yaw": 4},
            "p2": {"x": 11, "y": 12, "z": 13, "yaw": 14},
        })

        self.assertEqual(
            ["prepare", "start", "event", "decision", "finish"],
            [item["execution"]["phase"] for item in snapshots],
        )
        self.assertEqual(
            {"x": 1.0, "y": 2.0, "z": 3.0},
            snapshots[0]["world_state"]["vehicles"][0]["position"],
        )
        self.assertEqual(
            "failed", snapshots[2]["world_state"]["vehicles"][0]["health"]
        )
        self.assertEqual(
            {"x": 11.0, "y": 12.0, "z": 13.0},
            snapshots[-1]["world_state"]["vehicles"][0]["position"],
        )
        self.assertFalse(snapshots[-1]["execution"]["physical_execution"])

    def test_s08_golden_summary_uses_unified_scenario_contract(self):
        config = load_config(ROOT / "configs" / "mine_competition_demo.json")
        result = _normalize_s08_golden_summary({
            "mode": "carla-run",
            "status": "PASS",
            "run_id": "s08-test-run",
            "scenario_seed": 202608,
            "monitoring_dispatch_closed_loop": True,
            "risk_triggered": True,
            "risk_dataset_label": "synthetic_slope_competition",
            "risk_assessments": [{"level": "RED"}],
            "tasks": [{
                "task_id": "takeover-task-1",
                "task_type": "inspection",
                "status": "completed",
                "assigned_vehicle_id": "inspection_vehicle_02",
                "recommendation_reason": "safe feasible takeover",
            }],
            "vehicle_states": [{
                "vehicle_id": "inspection_vehicle_02",
                "available": True,
                "task_status": "idle",
            }],
        }, config)

        self.assertEqual("s08", result["scenario_spec"]["scenario_key"])
        self.assertEqual(
            "legacy_golden_compatibility_adapter",
            result["scenario_spec"]["implementation_mode"],
        )
        self.assertEqual(
            "progressive_slope_risk",
            result["scenario_events"][0]["event_type"],
        )
        self.assertEqual(1, result["metrics"]["completed_task_count"])
        self.assertEqual(
            "CLOSED_LOOP_PASS", result["closed_loop_validation"]["status"]
        )

    def test_unified_result_contract_is_shared_by_all_structural_scenarios(self):
        required = {
            "scenario_spec", "generated_episode", "scenario_events",
            "event_timeline", "decision_points", "decisions",
            "task_results", "route_plans", "metrics",
            "data_contract", "world_state", "closed_loop_validation",
            "concrete_episode_v2", "production_runtime",
        }
        for scenario in ("s01", "s02", "s03", "s04", "s05", "s06", "s07", "s09"):
            config = load_config(Path(SCENARIO_CATALOG[scenario]["compatibility_config_path"]))
            policy = "multi-objective" if scenario in {"s01", "s02"} else "heuristic"
            result = run_structural_scenario(
                scenario, config, seed=202608, random_map=True,
                vehicle_count=6, execution_policy=policy,
            )
            self.assertTrue(required.issubset(result), scenario)
            self.assertEqual(
                "openpit.scenario-spec.v1",
                result["scenario_spec"]["schema_version"],
            )
            self.assertEqual(
                "openpit.concrete-episode.v1",
                result["generated_episode"]["schema_version"],
            )
            production = result["production_runtime"]
            self.assertEqual("openpit.production-runtime.v1", production[
                "schema_version"])
            self.assertEqual("PASS", production["status"], scenario)
            self.assertEqual(6, production["completed_task_count"], scenario)
            self.assertEqual(6, production["task_count"], scenario)
            self.assertTrue(all(
                item["status"] == "COMPLETED_STRUCTURAL_SURROGATE"
                for item in production["tasks"]
            ), scenario)
            self.assertTrue(all(
                item["transitions"][-1]["to_status"] == "terminal"
                for item in production["tasks"]
            ), scenario)
            self.assertEqual(
                "COMMON_STRUCTURAL_RUNTIME_V1",
                result["concrete_episode_v2"]["execution_binding"][
                    "production_state_machine"
                ],
            )
            self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_validation"]["status"])

    def test_seeded_event_timing_is_reproducible_diverse_and_ordered(self):
        config_paths = {
            "s02": "s02_vehicle_failure_6v.json",
            "s03": "s03_loading_equipment_failure_6v.json",
            "s04": "s04_blasting_control_6v.json",
            "s05": "s05_extreme_weather_6v.json",
            "s06": "s06_congestion_6v.json",
            "s07": "s07_road_closure_6v.json",
            "s09": "s09_compound_road_fault_6v.json",
        }
        for scenario, filename in config_paths.items():
            config = load_config(ROOT / "configs" / filename)
            first = sample_event_timing(config, scenario, 202601)
            repeated = sample_event_timing(config, scenario, 202601)
            alternatives = {
                tuple(sorted(sample_event_timing(config, scenario, seed).items()))
                for seed in (202601, 202602, 202603)
            }
            self.assertEqual(first, repeated, scenario)
            self.assertGreater(len(alternatives), 1, scenario)
            if scenario == "s04":
                self.assertLess(first["notice_tick"], first["blast_start_tick"])
                self.assertLess(first["blast_start_tick"], first["clearance_tick"])
            elif scenario == "s09":
                self.assertLess(first["road_closure_tick"], first["vehicle_failure_tick"])
                self.assertLess(first["vehicle_failure_tick"], first["recovery_tick"])
            elif scenario == "s02":
                self.assertGreater(first["failure_tick"], 0)
            else:
                trigger = first.get("failure_tick", first.get(
                    "event_tick", first.get("closure_tick")
                ))
                self.assertLess(trigger, first["recovery_tick"], scenario)

    def test_incident_result_exposes_operator_decision_point_without_fake_approval(self):
        result = run_structural_scenario(
            "s02", load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        self.assertEqual("openpit.event-timeline.v1", result[
            "event_timeline"]["schema_version"])
        self.assertEqual(result["scenario_events"], result[
            "generated_episode"]["event_timeline"]["events"])
        event_points = [
            item for item in result["decision_points"]
            if item.get("trigger_event_id")
        ]
        self.assertEqual(1, len(event_points))
        point = event_points[0]
        self.assertEqual("REQUIRED_BEFORE_EXECUTION", point["review_policy"])
        self.assertEqual(
            "NOT_APPLICABLE_STRUCTURAL_SIMULATION", point["review_status"]
        )
        self.assertEqual("STRUCTURALLY_EVALUATED", point["execution_status"])
        self.assertTrue(point["candidate_actions"])
        self.assertIsNone(point["operator_response"])

    def test_compound_event_timeline_and_decisions_keep_event_order(self):
        result = run_structural_scenario(
            "s09", load_config(ROOT / "configs" / "s09_compound_road_fault_6v.json"),
            seed=202609, random_map=True, vehicle_count=6,
        )
        events = result["event_timeline"]["events"]
        self.assertEqual(
            ["road_closure", "vehicle_failure"],
            [item["event_type"] for item in events],
        )
        self.assertLess(
            events[0]["trigger"]["tick"], events[1]["trigger"]["tick"]
        )
        event_points = [
            item for item in result["decision_points"]
            if item.get("trigger_event_id")
        ]
        self.assertEqual(2, len(event_points))
        self.assertTrue(all(item["candidate_actions"] for item in event_points))

    def test_unified_contract_populates_existing_episode_tables(self):
        result = run_structural_scenario(
            "s02", load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(
                Path(directory) / "artifacts" / "runs", "unified-s02-test"
            )
            try:
                result["run_id"] = recorder.run_id
                result["generated_episode"]["run_id"] = recorder.run_id
                self.assertEqual(1, recorder.record_unified_scenario_contract(
                    result, ROOT / "configs" / "s02_vehicle_failure_6v.json"
                ))
                connection = recorder.store.connection
                self.assertEqual(6, connection.execute(
                    "SELECT count(*) FROM run_vehicles WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertEqual(6, connection.execute(
                    "SELECT count(*) FROM episode_tasks WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertGreaterEqual(connection.execute(
                    "SELECT count(*) FROM events WHERE run_id=? "
                    "AND event_type='decision_point_created'",
                    (recorder.run_id,),
                ).fetchone()[0], 1)
                self.assertGreater(connection.execute(
                    "SELECT count(*) FROM task_transitions WHERE run_id=? "
                    "AND event_type='task_production_stage_changed'",
                    (recorder.run_id,),
                ).fetchone()[0], 0)
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM episode_events WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0])
                self.assertTrue((recorder.run_dir / "scenario_contract.json").is_file())
            finally:
                recorder.close()

    def test_p6_duration_budget_prevents_short_execution_cutoff(self):
        workload = {
            "task_drafts": [{"from_point_id": "a", "to_point_id": "b"}],
            "generation": {"metadata": {}},
        }
        ticks, detail = _recommended_execution_ticks(
            workload, "s01", [{
                "from_point_id": "a", "to_point_id": "b",
                "validation_status": "PHYSICAL_REACHED",
                "duration_seconds": 118.5,
            }]
        )
        self.assertGreater(ticks, 3000)
        self.assertEqual("P6_FLEET_DURATION_SURROGATE", detail["status"])

    def test_p5_route_lengths_produce_a_conservative_fleet_budget(self):
        workload = {
            "task_drafts": [
                {"task_id": "task-a", "vehicle_id": "truck-a",
                 "from_point_id": "a", "to_point_id": "b",
                 "route_length_m": 300.0},
                {"task_id": "task-b", "vehicle_id": "truck-b",
                 "from_point_id": "c", "to_point_id": "d",
                 "route_length_m": 600.0},
            ],
            "generation": {"metadata": {}},
        }
        ticks, detail = _recommended_execution_ticks(
            workload, "s04", [], physical_route_capacity=1
        )
        self.assertGreater(ticks, 8000)
        self.assertEqual(
            "P5_ROUTE_LENGTH_FLEET_DURATION_SURROGATE", detail["status"]
        )
        self.assertEqual(0, detail["matched_route_count"])
        self.assertEqual(2, detail["estimated_route_count"])
        self.assertEqual(3.0, detail["p5_budget_speed_mps"])

    def test_fleet_launch_schedule_uses_deterministic_headway(self):
        workload = {"task_drafts": [
            {"vehicle_id": "truck-{}".format(index), "task_id": "task-{}".format(index)}
            for index in range(6)
        ]}
        schedule = _fleet_launch_schedule(workload, route_edge_plans={
            "task-0": ["road-a"],
            "task-1": ["road-b"],
            "task-2": ["road-a", "road-c"],
        })
        self.assertEqual([0, 160, 320, 480, 640, 800],
                         [item["launch_tick"] for item in schedule])
        self.assertTrue(all(
            item["basis"] == "fleet_size_aware_initial_staging"
            for item in schedule
        ))
        self.assertEqual(["task-0"], schedule[2]["topology_conflicts_with_task_ids"])
        self.assertIsNone(
            schedule[0]["physical_concurrent_route_capacity"]
        )
        self.assertEqual(
            "STAGGERED_HEADWAY_WITH_RUNTIME_RIGHT_OF_WAY",
            schedule[0]["runtime_admission_strategy"],
        )

    def test_route_admission_releases_each_vehicle_after_its_headway(self):
        class Adapter:
            def __init__(self):
                self.paused = []
                self.resumed = []

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {}

            def resume_vehicle(self, vehicle_id):
                self.resumed.append(vehicle_id)
                return {}

        tasks = [
            SimpleNamespace(
                task_id="task-1", assigned_vehicle_id="truck-1",
                status="executing",
            ),
            SimpleNamespace(
                task_id="task-2", assigned_vehicle_id="truck-2",
                status="executing",
            ),
        ]
        schedule = [
            {"task_id": "task-1", "launch_tick": 0},
            {"task_id": "task-2", "launch_tick": 160},
        ]
        adapter = Adapter()
        state = {"held_vehicle_ids": []}

        _reconcile_route_admission(
            tasks, adapter, schedule, state, tick_index=0
        )
        self.assertEqual(["truck-2"], adapter.paused)
        self.assertEqual(["truck-2"], state["held_vehicle_ids"])

        _reconcile_route_admission(
            tasks, adapter, schedule, state, tick_index=160
        )
        self.assertEqual(["truck-2"], adapter.resumed)
        self.assertEqual([], state["held_vehicle_ids"])
        self.assertEqual(
            ["task-1", "task-2"], state["active_task_ids"]
        )

    def test_route_admission_releases_shared_edge_after_headway(self):
        class Adapter:
            def __init__(self):
                self.paused = []
                self.resumed = []

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {}

            def resume_vehicle(self, vehicle_id):
                self.resumed.append(vehicle_id)
                return {}

        first = SimpleNamespace(
            task_id="task-1", assigned_vehicle_id="truck-1",
            status="executing",
        )
        second = SimpleNamespace(
            task_id="task-2", assigned_vehicle_id="truck-2",
            status="executing",
        )
        schedule = [
            {"task_id": "task-1", "launch_tick": 0,
             "topology_conflicts_with_task_ids": []},
            {"task_id": "task-2", "launch_tick": 100,
             "topology_conflicts_with_task_ids": ["task-1"]},
        ]
        adapter = Adapter()
        state = {"held_vehicle_ids": []}

        _reconcile_route_admission(
            [first, second], adapter, schedule, state, tick_index=0
        )
        self.assertEqual(["truck-2"], state["held_vehicle_ids"])
        _reconcile_route_admission(
            [first, second], adapter, schedule, state, tick_index=100
        )
        self.assertEqual([], state["held_vehicle_ids"])
        self.assertEqual(["truck-2"], adapter.resumed)

    def test_waiting_recovery_task_is_reassigned_when_capacity_is_idle(self):
        waiting = SimpleNamespace(
            task_id="waiting-task", assigned_vehicle_id=None,
            status="waiting_recovery_capacity", priority=80,
            required_capabilities=["haul"], original_vehicle_id="truck-1",
            recovery_wait_started_tick=700,
        )

        class RecoveryCapacityAdapter:
            def __init__(self):
                self.reassigned = []
                self.events = []

            def list_states(self):
                return [SimpleNamespace(
                    vehicle_id="truck-2", capabilities=["haul"],
                    available=True, health="healthy", task_status="idle",
                )]

            def reassign_task(self, task_id, vehicle_id,
                              assignment_source="manual_dispatch"):
                waiting.assigned_vehicle_id = vehicle_id
                waiting.status = "executing"
                self.reassigned.append(
                    (task_id, vehicle_id, assignment_source)
                )
                return {
                    "task_id": task_id, "vehicle_id": vehicle_id,
                    "status": "executing",
                }

            def _emit(self, event_type, payload):
                self.events.append((event_type, payload))

        adapter = RecoveryCapacityAdapter()
        attempts = {}
        decisions = []
        controls = _retry_waiting_recovery_tasks(
            [waiting], 900, adapter, {}, attempts,
            decision_points=decisions, scenario_key="s02",
            policy_version="MultiObjectiveCostModel-V1",
        )

        self.assertEqual("executing", waiting.status)
        self.assertEqual("truck-2", waiting.assigned_vehicle_id)
        self.assertEqual({"waiting-task": 1}, attempts)
        self.assertEqual(
            [("waiting-task", "truck-2", "scenario_recovery_capacity")],
            adapter.reassigned,
        )
        self.assertEqual(200, controls[0]["recovery_wait_ticks"])
        self.assertEqual(1, len(decisions))
        self.assertEqual("s02", decisions[0]["scenario_key"])
        self.assertEqual(
            "MultiObjectiveCostModel-V1", decisions[0]["policy_version"]
        )
        self.assertEqual(
            "APPROVED", decisions[0]["safety_review"]["status"]
        )
        self.assertEqual(
            "truck-2", decisions[0]["recommended_vehicle_id"]
        )
        self.assertEqual("EXECUTING", decisions[0]["execution_status"])
        self.assertEqual(
            "runtime_recovery_capacity_became_available",
            adapter.events[0][0],
        )

    def test_runtime_traffic_holds_rear_vehicle_then_releases_it(self):
        class Adapter:
            def __init__(self):
                self.paused = []
                self.resumed = []
                self.events = []

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {"task_id": "task-{}".format(vehicle_id[-1])}

            def resume_vehicle(self, vehicle_id):
                self.resumed.append(vehicle_id)
                return {"task_id": "task-{}".format(vehicle_id[-1])}

            def _emit(self, event_type, payload):
                self.events.append((event_type, payload))

        tasks = [
            SimpleNamespace(
                task_id="task-1", assigned_vehicle_id="truck-1",
                status="executing", priority=50, last_distance_m=200.0,
            ),
            SimpleNamespace(
                task_id="task-2", assigned_vehicle_id="truck-2",
                status="executing", priority=50, last_distance_m=150.0,
            ),
        ]
        position = lambda x: SimpleNamespace(x=x, y=0.0, z=0.0)
        moving_states = [
            SimpleNamespace(
                vehicle_id="truck-1", current_task_id="task-1",
                task_status="executing", available=True, health="healthy",
                position=position(0.0), yaw_deg=0.0, speed_mps=3.0,
            ),
            SimpleNamespace(
                vehicle_id="truck-2", current_task_id="task-2",
                task_status="executing", available=True, health="healthy",
                position=position(12.0), yaw_deg=0.0, speed_mps=2.0,
            ),
        ]
        adapter, state = Adapter(), {}
        controls = _reconcile_runtime_traffic(
            tasks, moving_states, adapter, state, tick_index=20
        )
        self.assertEqual(["truck-1"], adapter.paused)
        self.assertEqual(
            "front_vehicle_clears_shared_lane",
            controls[0]["decision_reason"],
        )
        self.assertEqual("truck-2", controls[0]["right_of_way_vehicle_id"])

        cleared_states = [
            SimpleNamespace(
                vehicle_id="truck-1", current_task_id="task-1",
                task_status="paused", available=True, health="healthy",
                position=position(0.0), yaw_deg=0.0, speed_mps=0.0,
            ),
            SimpleNamespace(
                vehicle_id="truck-2", current_task_id="task-2",
                task_status="executing", available=True, health="healthy",
                position=position(50.0), yaw_deg=0.0, speed_mps=3.0,
            ),
        ]
        _reconcile_runtime_traffic(
            tasks, cleared_states, adapter, state, tick_index=80
        )
        self.assertEqual(["truck-1"], adapter.resumed)
        self.assertEqual({}, state["holds"])
        summary = _runtime_traffic_summary(state, supported=True)
        self.assertEqual(1, summary["detected_conflict_count"])
        self.assertEqual(1, summary["resolved_conflict_count"])

    def test_runtime_traffic_uses_task_priority_for_converging_vehicles(self):
        class Adapter:
            def __init__(self):
                self.paused = []

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {}

        tasks = [
            SimpleNamespace(
                task_id="normal", assigned_vehicle_id="normal-truck",
                status="executing", priority=20, last_distance_m=100.0,
            ),
            SimpleNamespace(
                task_id="urgent", assigned_vehicle_id="urgent-truck",
                status="executing", priority=100, last_distance_m=300.0,
            ),
        ]
        states = [
            SimpleNamespace(
                vehicle_id="normal-truck", current_task_id="normal",
                task_status="executing", available=True, health="healthy",
                position=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                yaw_deg=0.0, speed_mps=2.0,
            ),
            SimpleNamespace(
                vehicle_id="urgent-truck", current_task_id="urgent",
                task_status="executing", available=True, health="healthy",
                position=SimpleNamespace(x=20.0, y=0.0, z=0.0),
                yaw_deg=180.0, speed_mps=2.0,
            ),
        ]
        adapter, state = Adapter(), {}
        controls = _reconcile_runtime_traffic(
            tasks, states, adapter, state, tick_index=20
        )
        self.assertEqual(["normal-truck"], adapter.paused)
        self.assertEqual("urgent-truck", controls[0]["right_of_way_vehicle_id"])
        self.assertEqual("higher_task_priority", controls[0]["decision_reason"])

    def test_runtime_traffic_never_overrides_existing_hold_owner(self):
        class Adapter:
            def __init__(self):
                self.paused = []

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {}

        tasks = [
            SimpleNamespace(
                task_id="task-1", assigned_vehicle_id="truck-1",
                status="executing", priority=50, last_distance_m=100.0,
            ),
            SimpleNamespace(
                task_id="task-2", assigned_vehicle_id="truck-2",
                status="executing", priority=50, last_distance_m=100.0,
            ),
        ]
        states = [
            SimpleNamespace(
                vehicle_id="truck-1", current_task_id="task-1",
                task_status="executing", available=True, health="healthy",
                position=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                yaw_deg=0.0, speed_mps=2.0,
            ),
            SimpleNamespace(
                vehicle_id="truck-2", current_task_id="task-2",
                task_status="executing", available=True, health="healthy",
                position=SimpleNamespace(x=15.0, y=0.0, z=0.0),
                yaw_deg=180.0, speed_mps=2.0,
            ),
        ]
        adapter = Adapter()
        controls = _reconcile_runtime_traffic(
            tasks, states, adapter, {}, tick_index=20,
            protected_vehicle_ids=["truck-1"],
        )
        self.assertEqual([], controls)
        self.assertEqual([], adapter.paused)

    def test_runtime_budget_accounts_for_reassigned_vehicle_queue(self):
        workload = {
            "task_drafts": [
                {"task_id": "task-a", "vehicle_id": "truck-a",
                 "from_point_id": "a", "to_point_id": "b"},
                {"task_id": "task-b", "vehicle_id": "truck-b",
                 "from_point_id": "c", "to_point_id": "d"},
            ],
            "generation": {"metadata": {}},
        }
        validations = [
            {"from_point_id": "a", "to_point_id": "b",
             "validation_status": "PHYSICAL_REACHED", "duration_seconds": 100.0},
            {"from_point_id": "c", "to_point_id": "d",
             "validation_status": "PHYSICAL_REACHED", "duration_seconds": 100.0},
        ]
        ticks, detail = _recommended_execution_ticks(
            workload, "s02", validations,
            assignments={"task-a": "truck-b", "task-b": "truck-b"},
        )
        self.assertGreater(ticks, 5000)
        self.assertEqual("P6_FLEET_DURATION_SURROGATE", detail["status"])
        self.assertEqual(200.0, detail["queue_duration_seconds_by_vehicle"]["truck-b"])

    def test_closed_loop_coordinator_orders_stages_and_applies_next_state(self):
        observed_order = []

        def stage(name, payload):
            def run(context):
                observed_order.append(name)
                return dict(payload)
            return run

        coordinator = ClosedLoopCoordinator(
            RuntimeState(),
            risk_stage=stage("risk", {"level": "BLUE"}),
            decision_stage=stage("decision", {"action": "ASSIGN"}),
            scheduling_stage=stage("scheduling", {
                "task_id": "task-1", "vehicle_id": "truck-1",
            }),
            planning_stage=stage("planning", {
                "schema_version": "openpit.route-plan.v1",
                "status": "PLANNED",
            }),
            safety_stage=stage("safety", {"status": "APPROVED"}),
            execution_stage=lambda context: {
                "schema_version": "openpit.execution-feedback.v1",
                "command_id": "command-1", "phase": "terminal",
                "status": "SUCCEEDED", "adapter_type": "TestAdapter",
                "physical_execution": False,
                "measurement_status": "STRUCTURAL_ONLY_NO_PHYSICS",
                "safety_gate_status": context["safety"]["status"],
                "task_states": [{
                    "task_id": "task-1", "status": "completed",
                    "assigned_vehicle_id": "truck-1",
                }],
                "vehicle_states": [{
                    "vehicle_id": "truck-1", "status": "idle",
                }],
                "events": [], "timestamp": "2026-09-06T12:00:00Z",
            },
        )
        result = coordinator.run_cycle({
            "run_id": "cycle-run-1",
            "vehicles": [{"vehicle_id": "truck-1", "status": "idle"}],
            "tasks": [{"task_id": "task-1", "status": "pending"}],
            "roads": {}, "environment": {}, "monitoring": {},
            "risk": {}, "traffic": {}, "equipment": {},
        })

        self.assertEqual(
            ["risk", "decision", "scheduling", "planning", "safety"],
            observed_order,
        )
        self.assertEqual("SUCCEEDED", result["status"])
        self.assertEqual(0, result["revision_before"])
        self.assertEqual(1, result["revision_after"])
        self.assertEqual(
            "completed", result["next_state"]["tasks"][0]["status"]
        )
        self.assertEqual(
            ["state", "risk", "decision", "scheduling", "planning",
             "safety", "execution", "feedback"],
            [item["stage"] for item in result["trace"]],
        )

    def test_closed_loop_coordinator_blocks_rejected_action(self):
        execution_calls = []
        constant = lambda value: lambda context: dict(value)
        coordinator = ClosedLoopCoordinator(
            RuntimeState(),
            risk_stage=constant({"level": "RED"}),
            decision_stage=constant({"action": "ENTER_HAZARD"}),
            scheduling_stage=constant({"vehicle_id": "truck-1"}),
            planning_stage=constant({"status": "PLANNED"}),
            safety_stage=constant({"status": "REJECTED"}),
            execution_stage=lambda context: execution_calls.append(context),
        )
        result = coordinator.run_cycle({
            "vehicles": [{"vehicle_id": "truck-1"}], "tasks": [],
        })

        self.assertEqual("BLOCKED_BY_SAFETY", result["status"])
        self.assertEqual([], execution_calls)
        self.assertEqual(0, result["revision_after"])
        self.assertEqual([], result["execution_feedback"])

    def test_scenario_catalog_does_not_advertise_unimplemented_scenarios(self):
        self.assertEqual(
            ["structural", "carla"], SCENARIO_CATALOG["s01"]["modes"]
        )
        self.assertEqual(["structural", "carla"], SCENARIO_CATALOG["s03"]["modes"])
        self.assertEqual(["structural", "carla"], SCENARIO_CATALOG["s04"]["modes"])
        self.assertEqual(["structural", "carla"], SCENARIO_CATALOG["s06"]["modes"])
        self.assertEqual(["carla"], SCENARIO_CATALOG["s08"]["modes"])
        self.assertEqual("IMPLEMENTED", SCENARIO_CATALOG["s09"]["status"])
        self.assertEqual(["structural", "carla"], SCENARIO_CATALOG["s09"]["modes"])
        self.assertEqual("RESOURCE_GATED", SCENARIO_CATALOG["s02"]["carla_readiness"])

    def test_shell_entry_reports_scope_without_starting_carla(self):
        completed = subprocess.run(
            [str(ROOT / "run_scenario.sh"), "--help"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("s01–s09统一场景入口", completed.stdout)
        self.assertIn("s08当前由边坡Golden兼容适配器执行", completed.stdout)
        self.assertIn("s01–s09统一场景入口", completed.stdout)
        self.assertIn("s06道路拥堵", completed.stdout)
        self.assertIn(
            "S03/S04/S05/S06/S07/S09使用真实地图资源",
            completed.stdout,
        )

    def test_shell_entry_rejects_unknown_carla_scenario(self):
        completed = subprocess.run(
            [str(ROOT / "run_scenario.sh"), "--scenario", "s00",
             "--mode", "carla"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("CARLA统一入口支持s01–s07/s09", completed.stderr)

    def test_s01_carla_bridge_executes_and_cleans_up_through_adapter_contract(self):
        class FakeExecutionAdapter:
            last_instance = None

            def __init__(self, config, load_map=False):
                self.config = config
                self.load_map = load_map
                self.tasks = []
                self.destroyed = False
                self.closed = False
                FakeExecutionAdapter.last_instance = self

            def connect(self):
                return None

            def ensure_vehicles(self, spawn_missing):
                self.spawn_missing = spawn_missing

            def resolve_zones(self, zones):
                return zones

            def dispatch(self, tasks, zones):
                self.tasks = list(tasks)
                for task in self.tasks:
                    task.status = "executing"
                    task.attempt_count += 1

            def tick(self):
                for task in self.tasks:
                    task.status = "completed"
                    task.status_reason = "fake_adapter_completed"
                return {}

            def drain_events(self):
                return []

            def list_states(self):
                return [SimpleNamespace(
                    vehicle_id=item.vehicle_id,
                    role_name=item.role_name,
                    health="healthy", available=True,
                    task_status="completed", current_task_id=None,
                ) for item in self.config.vehicles]

            def destroy_spawned_vehicles(self):
                self.destroyed = True
                return len(self.config.vehicles)

            def close(self):
                self.closed = True

        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_s01_carla_execution(
            config, seed=202601, vehicle_count=6, ticks=2,
            physical_route_pairs=planner_pairs_for_fake_carla(config),
            adapter_factory=FakeExecutionAdapter,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(6, result["completed_task_count"])
        self.assertEqual(6, result["destroyed_vehicle_count"])
        self.assertEqual(
            "openpit.execution-command.v1",
            result["execution_commands"][0]["schema_version"],
        )
        self.assertEqual(2, len(result["execution_feedback"]))
        self.assertTrue(all(
            item["physical_execution"] for item in result["execution_feedback"]
        ))
        self.assertTrue(all(
            item["measurement_status"] == "CARLA_MEASURED"
            for item in result["execution_feedback"]
        ))
        self.assertEqual(
            "openpit.decision-experience.v1",
            result["data_contract"]["decision_experience"],
        )
        self.assertEqual(1, len(result["decision_experiences"]))
        experience = result["decision_experiences"][0]
        self.assertEqual("initial_dispatch", experience["transition_kind"])
        self.assertEqual("CARLA_MEASURED", experience["measurement_status"])
        self.assertEqual("NOT_AVAILABLE_NO_REWARD_MODEL", experience["reward_status"])
        self.assertIsNone(experience["reward"])
        self.assertEqual(6, len(experience["route_context"]))
        self.assertTrue(FakeExecutionAdapter.last_instance.destroyed)
        self.assertTrue(FakeExecutionAdapter.last_instance.closed)

    def test_event_carla_admission_fails_closed_instead_of_falling_back_to_p5(self):
        class CheckOnlyAdapter:
            def __init__(self, config, load_map=False):
                self.config = config
                self.closed = False

            def connect(self):
                return None

            def close(self):
                self.closed = True

        configs = {
            "s04": "s04_blasting_control_6v.json",
            "s06": "s06_congestion_6v.json",
            "s07": "s07_road_closure_6v.json",
            "s09": "s09_compound_road_fault_6v.json",
        }
        for scenario_key, filename in configs.items():
            with self.subTest(scenario=scenario_key):
                config = load_config(ROOT / "configs" / filename)
                planner_pairs = sorted(planner_pairs_for_fake_carla(config))
                shared_origin = planner_pairs[0][0]
                insufficient = {
                    pair for pair in planner_pairs
                    if pair[0] == shared_origin
                }
                self.assertGreaterEqual(len(insufficient), 6)
                with self.assertRaisesRegex(
                    ValueError, "P6_ROUTE_ADMISSION_FAILED"
                ):
                    run_carla_scenario_execution(
                        scenario_key, config, seed=202601, vehicle_count=6,
                        ticks=10, check_only=True,
                        physical_route_pairs=insufficient,
                        adapter_factory=CheckOnlyAdapter,
                    )

    def test_carla_interruption_returns_partial_evidence_and_cleans_up(self):
        class InterruptedAdapter(RecordingScenarioAdapter):
            last_instance = None

            def __init__(self, config, load_map=False):
                super().__init__(config, load_map=load_map)
                InterruptedAdapter.last_instance = self

            def tick(self):
                self.tick_count += 1
                if self.tick_count >= 2:
                    raise KeyboardInterrupt()
                return super().tick()

        config = load_config(ROOT / "configs" / "s01_normal_6v.json")

        result = run_carla_scenario_execution(
            "s01", config, seed=202601, vehicle_count=6, ticks=10,
            physical_route_pairs=physical_pairs_for_fake_carla(config),
            adapter_factory=InterruptedAdapter,
        )

        self.assertEqual("INTERRUPTED", result["status"])
        self.assertTrue(result["interrupted"])
        self.assertEqual("operator_interrupted_run", result["interruption_reason"])
        self.assertTrue(all(
            item["status"] in {"completed", "cancelled"}
            for item in result["tasks"]
        ))
        self.assertIn(
            ("destroy_spawned_vehicles", None),
            InterruptedAdapter.last_instance.operations,
        )
        self.assertIn(
            ("close", None), InterruptedAdapter.last_instance.operations
        )

    def test_all_unified_scenarios_execute_their_carla_adapter_contract(self):
        expected_operations = {
            "s01": set(),
            # S02 has a configured 12 m pull-over and therefore keeps its
            # faulted actor; S09 has no validated parking resource and uses
            # the common finite-episode retirement fallback.
            "s02": {"inject_fault", "reassign_task"},
            "s03": {"retarget_task", "set_task_route"},
            "s04": {"pause_vehicle", "resume_vehicle"},
            "s05": {"set_task_speed_limit", "set_task_route"},
            "s06": {"pause_vehicle", "resume_vehicle"},
            "s07": {"pause_vehicle", "resume_vehicle"},
            "s09": {
                "inject_fault", "retire_vehicle", "reassign_task",
                "set_task_route",
            },
        }
        for scenario_key, required_operations in expected_operations.items():
            with self.subTest(scenario=scenario_key):
                config = load_config(Path(
                    SCENARIO_CATALOG[scenario_key]["compatibility_config_path"]
                ))
                result = run_carla_scenario_execution(
                    scenario_key, config, seed=202601, vehicle_count=6,
                    ticks=1200, execution_policy=(
                        "multi-objective"
                        if scenario_key in {"s01", "s02"}
                        else "heuristic"
                    ),
                    # Exercise orchestration with the same exact P6-success
                    # pool used by real CARLA admission.  The deliberately
                    # insufficient fail-closed fixture is covered separately.
                    physical_route_pairs=physical_pairs_for_fake_carla(config),
                    adapter_factory=RecordingScenarioAdapter,
                    operator_reviewer=lambda point: "approve",
                )
                operations = {
                    name for name, _value
                    in RecordingScenarioAdapter.last_instance.operations
                }
                self.assertEqual("PASS", result["status"], scenario_key)
                self.assertEqual(6, result["completed_task_count"], scenario_key)
                self.assertTrue(all(
                    not item["requires_deadhead"]
                    and item["vehicle_spawn_point_id"]
                    == item["service_origin_point_id"]
                    for item in result["task_mission_plans"]
                ), scenario_key)
                if scenario_key in {"s07", "s09"}:
                    self.assertTrue(any(
                        item.get("action")
                        == "hold_for_unvalidated_temporary_detour"
                        for item in result["runtime_controls"]
                    ), scenario_key)
                if scenario_key == "s09":
                    self.assertTrue(result["road_clearance_applied"])
                    self.assertTrue(any(
                        item.get("action")
                        == "resume_p6_route_after_road_reopen"
                        for item in result["runtime_controls"]
                    ))
                task_types = {
                    item["task_id"]: item["task_type"]
                    for item in result["tasks"]
                }
                production_task_ids = {
                    item["task_id"]
                    for item in result["production_cycle_plans"]
                }
                self.assertEqual(
                    {
                        task_id for task_id, task_type in task_types.items()
                        if task_type == "haul_transport"
                    },
                    production_task_ids,
                    scenario_key,
                )
                self.assertTrue(all(
                    item["completion_after_dumping"]
                    and item["return_execution"]
                    == "SEPARATE_TASK_NOT_EXECUTED"
                    for item in result["production_cycle_plans"]
                ), scenario_key)
                self.assertTrue(
                    required_operations.issubset(operations),
                    "{} missing {} from {}".format(
                        scenario_key, required_operations - operations,
                        sorted(operations),
                    ),
                )
                if scenario_key != "s01":
                    self.assertTrue(
                        result["scenario_event_applied"], scenario_key
                    )
                self.assertIn("destroy_spawned_vehicles", operations)
                self.assertIn("close", operations)

    def test_s02_carla_bridge_applies_fault_and_task_takeover(self):
        class FakeEventAdapter:
            last_instance = None

            def __init__(self, config, load_map=False):
                self.config = config
                self.tasks = []
                self.tick_count = 0
                self.faults = []
                self.reassignments = []
                self.paused = []
                self.resumed = []
                FakeEventAdapter.last_instance = self

            def connect(self):
                pass

            def ensure_vehicles(self, spawn_missing):
                pass

            def resolve_zones(self, zones):
                return zones

            def dispatch(self, tasks, zones):
                self.tasks = list(tasks)
                for task in self.tasks:
                    task.status = "executing"
                    task.attempt_count += 1

            def inject_fault(self, vehicle_id):
                self.faults.append(vehicle_id)

            def reassign_task(self, task_id, vehicle_id, speed_limit_kmh=None):
                task = next(item for item in self.tasks if item.task_id == task_id)
                task.assigned_vehicle_id = vehicle_id
                task.status = "executing"
                self.reassignments.append((task_id, vehicle_id))
                return {"task_id": task_id, "vehicle_id": vehicle_id, "status": "executing"}

            def pause_vehicle(self, vehicle_id):
                self.paused.append(vehicle_id)
                return {"vehicle_id": vehicle_id, "status": "paused"}

            def resume_vehicle(self, vehicle_id):
                self.resumed.append(vehicle_id)
                return {"vehicle_id": vehicle_id, "status": "executing"}

            def tick(self):
                self.tick_count += 1
                if self.tick_count >= 4:
                    for task in self.tasks:
                        task.status = "completed"
                return {}

            def drain_events(self):
                return []

            def list_states(self):
                return [SimpleNamespace(
                    vehicle_id=item.vehicle_id, role_name=item.role_name,
                    health="fault" if item.vehicle_id in self.faults else "healthy",
                    available=item.vehicle_id not in self.faults,
                    task_status="completed", current_task_id=None,
                ) for item in self.config.vehicles]

            def destroy_spawned_vehicles(self):
                return len(self.config.vehicles)

            def close(self):
                pass

        config = load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json")
        snapshots = []
        result = run_carla_scenario_execution(
            "s02", config,
            seed=202601, vehicle_count=6, ticks=9,
            physical_route_pairs=planner_pairs_for_fake_carla(config),
            adapter_factory=FakeEventAdapter,
            runtime_publisher=snapshots.append,
            operator_reviewer=lambda point: "approve",
        )
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["scenario_event_applied"])
        self.assertEqual(1, len(FakeEventAdapter.last_instance.faults))
        self.assertEqual(1, len(FakeEventAdapter.last_instance.reassignments))
        # Five route-capacity holds plus the six-vehicle incident review hold.
        # Approval releases only the incident hold; route-capacity holds stay
        # active, while the selected takeover vehicle is admitted explicitly.
        self.assertEqual(11, len(FakeEventAdapter.last_instance.paused))
        self.assertGreaterEqual(len(FakeEventAdapter.last_instance.resumed), 2)
        self.assertTrue(any(
            item["execution"]["phase"] == "await_human_review"
            for item in snapshots
        ))
        self.assertEqual("APPROVED_BY_HUMAN", result["operator_review_status"])
        event_experience = next(
            item for item in result["decision_experiences"]
            if item["transition_kind"] == "scenario_event_response"
        )
        self.assertEqual(result["event_tick"], event_experience["decision_tick"])
        self.assertEqual(
            "APPROVED_BY_HUMAN",
            event_experience["action"]["operator_review_status"],
        )
        self.assertEqual("SUCCEEDED", result["closed_loop_cycle"]["status"])
        persisted_command = result["closed_loop_cycle"]["stage_results"][
            "scheduling"
        ]["command"]
        self.assertEqual("openpit.execution-command.v1", persisted_command["schema_version"])
        self.assertEqual("dispatch_tasks", persisted_command["action_type"])
        self.assertEqual(6, len(persisted_command["assignments"]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = EvidenceRecorder(
                root / "artifacts" / "runs", "s02-physical-cycle-export-test"
            )
            try:
                result["run_id"] = recorder.run_id
                self.assertEqual(1, recorder.record_closed_loop_cycle(result))
                summary = dict(result)
                summary["scenario_seed"] = result["seed"]
                summary["scenario_mode"] = "seeded_random_map"
                recorder.store.update_from_summary(recorder.run_id, summary)
            finally:
                recorder.close()
            manifest = export_closed_loop_transition_dataset(
                root / "data" / "database" / "openpit.db",
                root / "datasets", "s02-physical-cycle-export-test",
                run_ids=[result["run_id"]],
            )
        self.assertEqual(1, manifest["learning_readiness"]["physical_record_count"])
        self.assertEqual(
            1,
            manifest["learning_readiness"][
                "offline_optimization_analysis_record_count"
            ],
        )
        self.assertEqual(
            "READY_FOR_OFFLINE_ANALYSIS",
            manifest["learning_readiness"]["offline_analysis_status"],
        )

    def test_carla_bridge_publishes_live_runtime_contract(self):
        class FakeRuntimeAdapter:
            def __init__(self, config, load_map=False):
                self.config = config
                self.tasks = []
                self.tick_count = 0

            def connect(self):
                pass

            def ensure_vehicles(self, spawn_missing):
                pass

            def resolve_zones(self, zones):
                return zones

            def dispatch(self, tasks, zones):
                self.tasks = list(tasks)
                for task in self.tasks:
                    task.status = "executing"

            def tick(self):
                self.tick_count += 1
                if self.tick_count >= 12:
                    for task in self.tasks:
                        task.status = "completed"

            def drain_events(self):
                return []

            def list_states(self):
                return [SimpleNamespace(
                    vehicle_id=item.vehicle_id, role_name=item.role_name,
                    health="healthy", available=True,
                    task_status="completed", current_task_id=None,
                ) for item in self.config.vehicles]

            def destroy_spawned_vehicles(self):
                return len(self.config.vehicles)

            def close(self):
                pass

        snapshots = []
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_carla_scenario_execution(
            "s01", config, seed=202601, vehicle_count=6, ticks=20,
            physical_route_pairs=planner_pairs_for_fake_carla(config),
            adapter_factory=FakeRuntimeAdapter,
            runtime_publisher=snapshots.append,
        )
        self.assertEqual("PASS", result["status"])
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertTrue(all(
            item["execution"]["physical_execution"] for item in snapshots
        ))
        self.assertEqual(6, len(snapshots[-1]["world_state"]["vehicles"]))
        self.assertEqual("finish", snapshots[-1]["execution"]["phase"])
        execute_ticks = [
            item["execution"]["tick"] for item in snapshots
            if item["execution"]["phase"] == "execute"
        ]
        self.assertIn(10, execute_ticks)
        self.assertLess(
            len(result["runtime_telemetry"]), len(execute_ticks)
        )

    def test_runtime_execution_events_use_common_evidence_store(self):
        with tempfile.TemporaryDirectory() as td:
            evidence_root = Path(td) / "project" / "artifacts" / "runs"
            recorder = EvidenceRecorder(evidence_root, "s01-carla-test")
            try:
                count = recorder.record_runtime_events({
                    "mode": "carla_multi_vehicle_execution",
                    "events": [{
                        "event_type": "task_started",
                        "tick": 7,
                        "payload": {"task_id": "task-1", "status": "executing"},
                    }],
                })
                self.assertEqual(1, count)
                event = recorder.store.connection.execute(
                    "SELECT tick,event_type,payload_json FROM events WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()
                self.assertEqual(7, event[0])
                self.assertEqual("task_started", event[1])
                self.assertIn("carla_multi_vehicle_execution", event[2])
                transition_count = recorder.store.connection.execute(
                    "SELECT count(*) FROM task_transitions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0]
                self.assertEqual(1, transition_count)
                feedback_count = recorder.record_execution_feedback({
                    "execution_feedback": [{
                        "schema_version": "openpit.execution-feedback.v1",
                        "command_id": "command-1", "phase": "terminal",
                        "status": "SUCCEEDED",
                        "measurement_status": "CARLA_MEASURED",
                    }],
                })
                self.assertEqual(1, feedback_count)
                persisted = recorder.store.connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? "
                    "AND event_type='execution_feedback'",
                    (recorder.run_id,),
                ).fetchone()[0]
                self.assertEqual(
                    "openpit.execution-feedback.v1",
                    json.loads(persisted)["schema_version"],
                )
            finally:
                recorder.close()

    def test_carla_runtime_plan_indexes_common_decisions_and_routes(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = EvidenceRecorder(
                Path(td) / "artifacts" / "runs", "s04-carla-plan-test"
            )
            try:
                counts = recorder.record_unified_runtime_plan({
                    "decisions": [{
                        "schema_version": "openpit.decision-record.v1",
                        "decision_id": "decision-1",
                        "decision_type": "blast_response",
                        "action_type": "hold_until_blast_clearance",
                        "task_id": "task-1",
                        "selected_vehicle_id": "truck-1",
                        "score": 2.5,
                        "policy_version": "planned-blast-road-control-v1",
                        "constraint_results": {"closed_road_avoided": True},
                        "candidate_evaluations": [],
                    }],
                    "route_plans": [{
                        "route_plan_id": "task-1:replanned",
                        "vehicle_id": "truck-1", "task_id": "task-1",
                        "planner_version": "RoadGraph-Dijkstra-Topology-V1",
                        "start_node": "point-1", "goal_node": "point-2",
                        "distance_m": 120.0, "status": "PLANNED",
                    }],
                    "task_mission_plans": [{
                        "task_id": "task-1", "vehicle_id": "truck-1",
                        "vehicle_spawn_point_id": "point-0",
                        "service_origin_point_id": "point-1",
                        "service_target_point_id": "point-2",
                        "deadhead_route_length_m": 30.0,
                        "mission_route_length_m": 120.0,
                        "mission_route_evidence": "P6_PHYSICAL_REACHED",
                    }],
                })
                decision = recorder.store.connection.execute(
                    "SELECT context,task_id,vehicle_id,score,policy_version "
                    "FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()
                routes = recorder.store.connection.execute(
                    "SELECT route_plan_id,distance_m,status FROM route_plans "
                    "WHERE run_id=? ORDER BY route_plan_id",
                    (recorder.run_id,),
                ).fetchall()
                experience_count = recorder.record_decision_experiences({
                    "decision_experiences": [{
                        "schema_version": "openpit.decision-experience.v1",
                        "experience_id": "s04:event:20:1",
                        "scenario_key": "s04",
                        "transition_kind": "scenario_event_response",
                        "decision_tick": 20,
                        "measurement_status": "CARLA_MEASURED",
                        "state_before": {"tick": 20, "tasks": []},
                        "action": {"action_type": "hold_for_blast"},
                        "route_context": [],
                        "state_after": {"tick": 20, "tasks": []},
                        "reward": None,
                        "reward_status": "NOT_AVAILABLE_NO_REWARD_MODEL",
                        "done": False,
                    }],
                })
                experience_event = recorder.store.connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? "
                    "AND event_type='decision_experience_captured'",
                    (recorder.run_id,),
                ).fetchone()
                experience_artifact = (
                    recorder.run_dir / "decision_experiences.jsonl"
                ).exists()
            finally:
                recorder.close()

        self.assertEqual({
            "decision_count": 1,
            "route_plan_count": 1,
            "task_mission_plan_count": 1,
        }, counts)
        self.assertEqual(
            ("blast_response", "task-1", "truck-1", 2.5,
             "planned-blast-road-control-v1"),
            decision,
        )
        self.assertEqual([
            ("task-1:carla-mission", 150.0, "CARLA_MISSION_CONFIGURED"),
            ("task-1:replanned", 120.0, "PLANNED"),
        ], routes)
        self.assertEqual(1, experience_count)
        self.assertTrue(experience_artifact)
        self.assertEqual(
            "s04:event:20:1", json.loads(experience_event[0])["experience_id"]
        )

    def test_decision_experience_export_builds_observed_reward_intervals(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recorder = EvidenceRecorder(
                root / "artifacts" / "runs", "decision-reward-test"
            )
            run_id = recorder.run_id
            experiences = []
            for index, (kind, tick, status) in enumerate((
                ("initial_dispatch", 0, "assigned"),
                ("scenario_event_response", 10, "executing"),
            ), 1):
                experiences.append({
                    "schema_version": "openpit.decision-experience.v1",
                    "experience_id": "s02:{}:{}:{}".format(kind, tick, index),
                    "scenario_key": "s02", "transition_kind": kind,
                    "decision_tick": tick, "tick": tick,
                    "measurement_status": "CARLA_MEASURED",
                    "state_before": {"tick": tick, "tasks": [{
                        "task_id": "task-1", "status": status,
                        "assigned_vehicle_id": "truck-1",
                    }]},
                    "action": {
                        "action_type": "dispatch_tasks" if index == 1
                        else "apply_scenario_event_response",
                        "safety_gate_status": "APPROVED",
                    },
                    "route_context": [],
                    "state_after": {"tick": tick, "tasks": [{
                        "task_id": "task-1", "status": "executing",
                        "assigned_vehicle_id": "truck-1",
                    }]},
                    "reward": None,
                    "reward_status": "NOT_AVAILABLE_NO_REWARD_MODEL",
                    "done": False,
                })
            try:
                self.assertEqual(
                    2, recorder.record_decision_experiences({
                        "decision_experiences": experiences,
                    })
                )
                recorder.store.update_from_summary(run_id, {
                    "status": "PASS", "scenario_seed": 202604,
                    "scenario_mode": "seeded_random_map",
                    "mode": "carla_multi_scenario_execution",
                    "task_count": 1, "completed_task_count": 1,
                    "ticks_executed": 20, "effective_execution_ticks": 100,
                    "tasks": [{
                        "task_id": "task-1", "status": "completed",
                        "assigned_vehicle_id": "truck-1",
                    }],
                    "final_vehicle_states": [{
                        "vehicle_id": "truck-1", "task_status": "completed",
                    }],
                })
            finally:
                recorder.close()
            manifest = export_decision_experience_transition_dataset(
                root / "data" / "database" / "openpit.db",
                root / "data" / "datasets", "reward-v1-test",
                load_decision_experience_reward_config(
                    ROOT / "configs" / "dispatch_cost_v1.json"
                ), run_ids=[run_id],
            )
            records = [
                json.loads(line) for line in Path(
                    manifest["transitions_path"]
                ).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual("DATASET_QUALITY_PASS", manifest["dataset_quality"]["status"])
        self.assertEqual(2, manifest["record_count"])
        self.assertEqual(1, manifest["terminal_record_count"])
        self.assertEqual(0, manifest["ppo_eligible_record_count"])
        self.assertEqual(
            {"s02": 1}, manifest["coverage"]["scenario_run_counts"]
        )
        self.assertEqual(
            {"initial_dispatch": 1, "scenario_event_response": 1},
            manifest["coverage"]["transition_kind_counts"],
        )
        self.assertEqual(
            "BASELINE_DATA_AVAILABLE_MORE_SEEDS_REQUIRED",
            manifest["learning_readiness"]["status"],
        )
        self.assertEqual(
            "DISABLED_BY_DESIGN",
            manifest["learning_readiness"]["automatic_policy_update"],
        )
        self.assertFalse(records[0]["done"])
        self.assertTrue(records[1]["done"])
        self.assertTrue(all(item["reward"] is not None for item in records))
        self.assertEqual(
            "initial_equal_engineering_weights_not_validated_as_optimal",
            records[0]["reward_detail"]["weight_status"],
        )

    def test_s02_carla_database_validation_uses_runtime_event_names(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = EvidenceRecorder(
                Path(td) / "artifacts" / "runs", "s02-carla-test"
            )
            result = {
                "mode": "carla_multi_scenario_execution",
                "status": "PASS", "task_count": 1,
                "completed_task_count": 1, "reassignment_count": 1,
                "tasks": [{
                    "task_id": "task-1", "zone_id": "zone-1",
                    "task_type": "slope_inspection", "priority": 50,
                    "status": "completed", "assigned_vehicle_id": "truck-2",
                    "completed_tick": 100,
                }],
            }
            try:
                recorder.record_runtime_events({
                    "mode": result["mode"],
                    "events": [{"event_type": name, "payload": {}}
                    for name in (
                        "vehicle_fault_applied",
                        "task_suspended_for_preemption",
                        "task_reassigned_by_scenario",
                        "task_completed",
                    )],
                })
                recorder.store.update_from_summary(recorder.run_id, result)
                validation = recorder.store.validate_closed_loop_evidence(
                    recorder.run_id, "s02", result
                )
            finally:
                recorder.close()

        self.assertEqual("EVIDENCE_PASS", validation["status"])
        self.assertEqual([], validation["failed_checks"])

    def test_s04_s09_carla_database_validation_uses_unified_runtime_facts(self):
        cases = {
            "s04": (
                "scenario_control_applied",
                "scenario_control_recovered",
                "task_completed",
            ),
            "s09": (
                "scenario_control_applied",
                "vehicle_fault_applied",
                "task_suspended_for_preemption",
                "task_reassigned_by_scenario",
                "scenario_control_recovered",
                "task_completed",
            ),
        }
        for scenario_key, event_types in cases.items():
            with self.subTest(scenario_key=scenario_key):
                with tempfile.TemporaryDirectory() as td:
                    recorder = EvidenceRecorder(
                        Path(td) / "artifacts" / "runs",
                        "{}-carla-test".format(scenario_key),
                    )
                    result = {
                        "mode": "carla_multi_scenario_execution",
                        "status": "PASS", "task_count": 1,
                        "completed_task_count": 1,
                        "tasks": [{
                            "task_id": "task-1", "status": "completed",
                            "assigned_vehicle_id": "truck-1",
                        }],
                        "decisions": [{
                            "decision_id": "decision-1",
                            "decision_type": "event_response",
                            "action_type": "hold",
                            "task_id": "task-1",
                            "selected_vehicle_id": "truck-1",
                        }],
                        "route_plans": [{
                            "route_plan_id": "route-1", "task_id": "task-1",
                            "vehicle_id": "truck-1", "distance_m": 100.0,
                            "status": "PLANNED",
                        }],
                        "task_mission_plans": [{
                            "task_id": "task-1", "vehicle_id": "truck-1",
                            "vehicle_spawn_point_id": "point-1",
                            "service_target_point_id": "point-2",
                            "mission_route_length_m": 100.0,
                        }],
                    }
                    try:
                        recorder.record_unified_runtime_plan(result)
                        recorder.record_runtime_events({
                            "mode": result["mode"],
                            "events": [
                                {"event_type": item, "payload": {}}
                                for item in event_types
                            ],
                        })
                        recorder.store.update_from_summary(
                            recorder.run_id, result
                        )
                        validation = (
                            recorder.store.validate_closed_loop_evidence(
                                recorder.run_id, scenario_key, result
                            )
                        )
                    finally:
                        recorder.close()
                self.assertEqual("EVIDENCE_PASS", validation["status"])
                self.assertEqual([], validation["failed_checks"])

    def test_closed_loop_validator_rejects_count_only_false_pass(self):
        result = validate_structural_closed_loop({
            "scenario_key": "s01", "status": "PASS",
            "task_count": 2, "completed_task_count": 2,
            "tasks": [], "assignments": [],
        })
        self.assertEqual("CLOSED_LOOP_FAIL", result["status"])
        self.assertIn("task_terminal_states", result["failed_checks"])

    def test_batch_summary_uses_only_observed_counts(self):
        summary = summarize_structural_batch([
            {
                "scenario_key": "s02", "status": "PASS", "task_count": 6,
                "completed_task_count": 6, "released_task_ids": ["t1"],
                "reassignment_count": 1,
            },
            {
                "scenario_key": "s07", "status": "PASS", "task_count": 6,
                "completed_task_count": 6, "affected_task_count": 2,
                "replanned_task_count": 2, "same_vehicle_replan_count": 1,
                "takeover_count": 1,
            },
            {
                "scenario_key": "s03", "status": "PASS", "task_count": 6,
                "completed_task_count": 6, "affected_task_count": 1,
            },
        ])
        self.assertEqual("PASS", summary["status"])
        self.assertEqual(1.0, summary["task_completion_rate"])
        self.assertEqual(1.0, summary["by_scenario"]["s02"]["failure_reassignment_rate"])
        self.assertEqual(1.0, summary["by_scenario"]["s07"]["route_replan_success_rate"])
        self.assertIsNone(summary["by_scenario"]["s02"]["route_replan_success_rate"])
        self.assertIsNone(summary["by_scenario"]["s03"]["route_replan_success_rate"])

    def test_s01_keeps_existing_structural_contract(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_structural_scenario("s01", config, seed=202601)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual("unified_structural_runner_v1", result["runner_entry"])
        self.assertEqual("s01", result["scenario_key"])
        self.assertEqual(
            "openpit.scenario-run-result.v1", result["result_schema_version"]
        )
        self.assertEqual("structural", result["execution"]["simulator"])
        self.assertFalse(result["execution"]["physical_execution"])
        self.assertEqual(6, result["outcome"]["completed_task_count"])
        self.assertEqual("openpit.world-state.v1",
                         result["world_state"]["schema_version"])
        self.assertEqual(6, len(result["world_state"]["tasks"]))
        self.assertEqual(
            ["prepare", "start", "event", "decision", "execute", "feedback", "finish"],
            [item["phase"] for item in result["lifecycle"]["phases"]],
        )
        self.assertEqual("finish", result["lifecycle"]["current_phase"])
        self.assertEqual(
            "openpit.execution-command.v1",
            result["execution_commands"][0]["schema_version"],
        )
        self.assertEqual(2, len(result["execution_feedback"]))
        self.assertTrue(all(
            item["measurement_status"] == "STRUCTURAL_ONLY_NO_PHYSICS"
            for item in result["execution_feedback"]
        ))

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaises(ValueError):
            run_structural_scenario("s99", object())

    def test_structural_transition_keeps_unknown_measurements_null(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_structural_scenario("s01", config, seed=202601)
        result["run_id"] = "run-s01-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}

        records = build_structural_transition_dataset(result)

        self.assertEqual(result["assignment_count"], len(records))
        self.assertTrue(all(item["reward"] is None for item in records))
        self.assertTrue(all(item["done"] for item in records))
        self.assertTrue(all(
            item["data_quality"]["eligible_for_ppo_training"] is False
            for item in records
        ))
        self.assertEqual("0325_5", records[0]["map_context"]["map_id"])
        self.assertEqual(
            "openpit.world-state.v1", records[0]["state"]["schema_version"]
        )
        self.assertEqual(
            "openpit.world-state.v1", records[0]["next_state"]["schema_version"]
        )
        self.assertEqual(
            "partial_affected_task_scope",
            records[0]["state"]["snapshot_metadata"]["completeness"],
        )
        self.assertEqual(1, len(records[0]["state"]["tasks"]))
        self.assertEqual(
            "openpit.decision-action.v1",
            records[0]["action"]["schema_version"],
        )
        self.assertEqual(
            "openpit.execution-feedback.v1",
            records[0]["result"]["schema_version"],
        )

    def test_heuristic_and_optimized_policies_share_execution_safety_gate(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        shadow = run_structural_scenario(
            "s01", config, seed=202601, random_map=True, vehicle_count=6,
            execution_policy="heuristic",
        )
        shadow_records = build_structural_transition_dataset(shadow)
        self.assertEqual(
            "execution_gate",
            shadow["policy_comparison"]["safety_shield"]["mode"],
        )
        self.assertEqual("PASS", shadow["safety_shield"]["status"])
        self.assertIn("safety_review", shadow_records[0]["action"])
        self.assertEqual("SUCCEEDED", shadow["closed_loop_cycle"]["status"])
        self.assertEqual("executed", shadow["closed_loop_coordination"])

        executed = run_structural_scenario(
            "s01", config, seed=202601, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        executed_records = build_structural_transition_dataset(executed)
        self.assertEqual(
            "execution_gate",
            executed["policy_comparison"]["safety_shield"]["mode"],
        )
        self.assertEqual("PASS", executed["safety_shield"]["status"])
        self.assertIn("safety_review", executed_records[0]["action"])
        self.assertEqual(
            "APPROVED",
            executed["execution_commands"][0]["safety_gate_status"],
        )

    def test_event_scenarios_share_safety_shield_contract(self):
        config_names = {
            "s03": "s03_loading_equipment_failure_6v.json",
            "s04": "s04_blasting_control_6v.json",
            "s05": "s05_extreme_weather_6v.json",
            "s06": "s06_congestion_6v.json",
            "s07": "s07_road_closure_6v.json",
            "s09": "s09_compound_road_fault_6v.json",
        }
        for scenario, config_name in config_names.items():
            result = run_structural_scenario(
                scenario, load_config(ROOT / "configs" / config_name),
                seed=202601, random_map=True, vehicle_count=6,
            )
            shield = result["safety_shield"]
            self.assertEqual("PASS", shield["status"], scenario)
            self.assertGreater(shield["review_count"], 0, scenario)
            self.assertEqual(0, shield["rejected_count"], scenario)
            self.assertTrue(all(
                item["status"] == "APPROVED" for item in shield["reviews"]
            ), scenario)
            transitions = build_structural_transition_dataset(result)
            self.assertTrue(transitions, scenario)
            self.assertTrue(all(
                item["action"].get("safety_review", {}).get("status")
                == "APPROVED" for item in transitions
            ), scenario)
            self.assertEqual(
                "coordinated_structural_execution",
                result["execution_contract_mode"],
            )
            self.assertEqual(1, len(result["execution_commands"]))
            self.assertEqual(1, len(result["execution_feedback"]))
            self.assertEqual(
                "terminal",
                result["execution_feedback"][0]["phase"],
            )
            self.assertFalse(
                result["execution_feedback"][0]["physical_execution"]
            )
            self.assertEqual(
                "APPROVED",
                result["execution_commands"][0]["safety_gate_status"],
            )
            cycle = result["closed_loop_cycle"]
            self.assertEqual(
                "openpit.closed-loop-cycle.v1", cycle["schema_version"]
            )
            self.assertEqual("SUCCEEDED", cycle["status"])
            self.assertEqual(
                (0, 1), (cycle["revision_before"], cycle["revision_after"])
            )
            self.assertTrue(all(
                item.get("status") == "completed"
                for item in cycle["next_state"]["tasks"]
            ), scenario)
            if scenario in {"s04", "s05", "s07", "s09"}:
                self.assertTrue(all(
                    item["action"].get("route_contract", {}).get(
                        "schema_version"
                    ) == "openpit.route-plan.v1"
                    for item in transitions
                ), scenario)

    def test_s02_heuristic_uses_coordinated_structural_safety_gate(self):
        result = run_structural_scenario(
            "s02",
            load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(
            "coordinated_structural_execution",
            result["execution_contract_mode"],
        )
        self.assertEqual(
            "APPROVED",
            result["execution_commands"][0]["safety_gate_status"],
        )
        feedback = result["execution_feedback"][0]
        self.assertEqual("SUCCEEDED", feedback["status"])
        self.assertFalse(feedback["physical_execution"])
        self.assertEqual(
            "STRUCTURAL_ONLY_NO_PHYSICS", feedback["measurement_status"]
        )

    def test_s02_result_feedback_becomes_runtime_next_state(self):
        result = run_structural_scenario(
            "s02",
            load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
        )
        runtime = RuntimeState()
        runtime.sync_snapshot(result)

        state = runtime.get_state()
        self.assertEqual(2, state["state_revision"])
        self.assertEqual(6, len(state["tasks"]))
        self.assertTrue(all(
            task.get("status") == "completed" for task in state["tasks"]
        ))
        self.assertEqual(
            "SUCCEEDED",
            state["feedback"]["latest_execution_feedback"]["status"],
        )
        self.assertEqual(
            result["execution_feedback"][0]["command_id"],
            state["feedback"]["latest_execution_feedback"]["command_id"],
        )

    def test_safety_review_is_persisted_in_decision_payload(self):
        result = run_structural_scenario(
            "s03",
            load_config(ROOT / "configs" / "s03_loading_equipment_failure_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
        )
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(
                Path(directory) / "artifacts" / "runs", "s03-safety-db-test"
            )
            try:
                recorder.record_structural_events(result)
                payload = recorder.store.connection.execute(
                    "SELECT payload_json FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0]
            finally:
                recorder.close()
        decision = json.loads(payload)
        self.assertEqual("APPROVED", decision["safety_review"]["status"])
        self.assertTrue(decision["constraint_results"])

    def test_route_contract_is_persisted_in_decision_payload(self):
        result = run_structural_scenario(
            "s05", load_config(ROOT / "configs" / "s05_extreme_weather_6v.json"),
            seed=202601, random_map=True, vehicle_count=6,
        )
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(
                Path(directory) / "artifacts" / "runs", "s05-route-db-test"
            )
            try:
                recorder.record_structural_events(result)
                payload = recorder.store.connection.execute(
                    "SELECT payload_json FROM decisions WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0]
            finally:
                recorder.close()
        decision = json.loads(payload)
        self.assertEqual(
            "openpit.route-plan.v1",
            decision["route_contract"]["schema_version"],
        )
        self.assertIn(
            decision["route_contract"]["planning_status"],
            {"PLANNED", "UNREACHABLE"},
        )

    def test_dataset_writer_uses_one_schema_for_s01_s02_s07(self):
        results = []
        config_names = {
            "s01": "s01_normal_6v.json",
            "s02": "s02_vehicle_failure_6v.json",
            "s07": "s07_road_closure_6v.json",
        }
        for scenario, seed in (("s01", 202607), ("s02", 202607), ("s07", 202608)):
            config = load_config(ROOT / "configs" / config_names[scenario])
            result = run_structural_scenario(
                scenario, config, seed=seed, random_map=True, vehicle_count=6
            )
            result["run_id"] = "run-{}-test".format(scenario)
            result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
            results.append(result)
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_structural_transition_dataset(
                results, Path(directory), "dataset-test"
            )
            lines = Path(manifest["transitions_path"]).read_text(
                encoding="utf-8"
            ).splitlines()
        self.assertEqual(manifest["record_count"], len(lines))
        self.assertEqual(
            {"s01", "s02", "s07"}, set(manifest["scenario_record_counts"])
        )
        self.assertEqual(
            "NOT_AVAILABLE_NO_REWARD_MODEL", manifest["reward_status"]
        )
        self.assertEqual(0, manifest["reward_summary"]["available_record_count"])
        self.assertEqual(
            "DATASET_QUALITY_PASS", manifest["dataset_quality"]["status"]
        )
        self.assertEqual(
            {
                "state": "openpit.world-state.v1",
                "action": "openpit.decision-action.v1",
                "feedback": "openpit.execution-feedback.v1",
                "next_state": "openpit.world-state.v1",
            },
            manifest["transition_contract"],
        )
        self.assertEqual(
            0,
            manifest["dataset_quality"]["missing_critical_field_counts"][
                "state_schema_version"
            ],
        )
        self.assertEqual(2, manifest["dataset_quality"]["unique_seed_count"])

    def test_structural_reward_uses_only_observed_terminal_facts(self):
        reward_config = load_structural_reward_config(
            ROOT / "configs" / "dispatch_cost_v1.json"
        )
        config = load_config(ROOT / "configs" / "s07_road_closure_6v.json")
        result = run_structural_scenario(
            "s07", config, seed=202601, random_map=True, vehicle_count=6
        )
        result["run_id"] = "run-s07-reward-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}

        records = build_structural_transition_dataset(result, reward_config)

        self.assertTrue(records)
        self.assertTrue(all(item["reward"] is not None for item in records))
        self.assertTrue(all(
            item["reward_status"] == "STRUCTURAL_TERMINAL_REWARD_AVAILABLE"
            for item in records
        ))
        detail = records[0]["reward_detail"]
        self.assertEqual("structural-terminal-reward-v1", detail["reward_version"])
        self.assertEqual(
            "available",
            detail["components"]["switch_cost"]["availability"],
        )
        self.assertEqual(0.0, detail["components"]["switch_cost"]["value"])
        self.assertEqual(
            "available",
            detail["components"]["detour_cost"]["availability"],
        )
        self.assertFalse(
            records[0]["data_quality"]["eligible_for_ppo_training"]
        )

    def test_reward_dataset_manifest_reports_observed_reward_statistics(self):
        reward_config = load_structural_reward_config(
            ROOT / "configs" / "dispatch_cost_v1.json"
        )
        config = load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json")
        result = run_structural_scenario(
            "s02", config, seed=202607, random_map=True, vehicle_count=6
        )
        result["run_id"] = "run-s02-manifest-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_structural_transition_dataset(
                [result], Path(directory), "reward-dataset-test", reward_config
            )
        self.assertEqual(
            manifest["record_count"],
            manifest["reward_summary"]["available_record_count"],
        )
        self.assertIn("s02", manifest["reward_summary"]["scenario_means"])

    def test_dataset_quality_warns_when_expected_scenario_is_missing(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_structural_scenario("s01", config, seed=202601)
        result["run_id"] = "run-s01-coverage-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_structural_transition_dataset(
                [result], Path(directory), "coverage-test",
                expected_scenarios=("s01", "s02"),
            )
        quality = manifest["dataset_quality"]
        self.assertEqual("DATASET_QUALITY_WARN", quality["status"])
        self.assertEqual(["s02"], quality["missing_expected_scenarios"])

    def test_dataset_version_deduplicates_and_isolates_seeds(self):
        reward_config = load_structural_reward_config(
            ROOT / "configs" / "dispatch_cost_v1.json"
        )
        results = []
        config_names = {
            "s01": "s01_normal_6v.json",
            "s02": "s02_vehicle_failure_6v.json",
            "s07": "s07_road_closure_6v.json",
        }
        for scenario, seed in (("s01", 202701), ("s02", 202702), ("s07", 202703)):
            result = run_structural_scenario(
                scenario, load_config(ROOT / "configs" / config_names[scenario]),
                seed=seed, random_map=True, vehicle_count=6,
            )
            result["run_id"] = "run-{}-{}".format(scenario, seed)
            result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
            results.append(result)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_structural_transition_dataset(
                results, root, "structural-batch-a", reward_config,
                expected_scenarios=("s01", "s02", "s07"),
            )
            write_structural_transition_dataset(
                results, root, "structural-batch-b", reward_config,
                expected_scenarios=("s01", "s02", "s07"),
            )
            manifest = aggregate_structural_transition_datasets(
                root, "bc-dataset-test", split_seed=99,
            )
            split_rows = {}
            for name, detail in manifest["splits"].items():
                split_rows[name] = Path(detail["path"]).read_text(
                    encoding="utf-8"
                ).splitlines()
        self.assertTrue(manifest["status"].startswith("DATASET_VERSION_READY"))
        self.assertGreater(manifest["semantic_duplicate_count"], 0)
        self.assertTrue(all(not values for values in manifest["seed_overlap"].values()))
        self.assertEqual(
            manifest["record_count"], sum(len(items) for items in split_rows.values())
        )
        self.assertTrue(all(manifest["splits"][name]["seed_count"] >= 1
                            for name in ("train", "validation", "test")))

    def test_random_s01_uses_global_map_draft(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_structural_scenario("s01", config, seed=202601,
                                         random_map=True, vehicle_count=6)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual(6, len(result["map_resource_task_draft"]))
        self.assertTrue(all(
            item.get("spawn_point_id")
            and item.get("service_origin_point_id") == item.get("from_point_id")
            and item.get("service_target_point_id") == item.get("to_point_id")
            and item.get("deadhead_validation_status")
            for item in result["map_resource_task_draft"]
        ))
        self.assertTrue(any(
            item.get("requires_deadhead")
            for item in result["map_resource_task_draft"]
        ))
        self.assertEqual("map_resources_global_p5", result["scenario_source"])
        self.assertTrue(result["candidate_rankings"])
        self.assertTrue(all(
            "p5_route_length" in assignment["reason"]
            for assignment in result["assignments"]
        ))
        self.assertEqual(6, len({
            assignment["vehicle_id"] for assignment in result["assignments"]
        }))
        comparison = result["policy_comparison"]
        self.assertEqual("shadow_only_v1_not_executed", comparison["mode"])
        self.assertEqual(6, len(comparison["comparisons"]))
        first = comparison["comparisons"][0]["v1_candidate_ranking"][0]
        self.assertEqual("multi-objective-cost-v1", first["policy_version"])
        self.assertIn("cost_transport", first)
        self.assertIn("constraint_results", first)

    def test_random_s01_can_execute_multi_objective_policy(self):
        config = load_config(ROOT / "configs" / "s01_normal_6v.json")
        result = run_structural_scenario(
            "s01", config, seed=202607, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        comparison = result["policy_comparison"]
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual("multi-objective-cost-v1", comparison["executed_policy"])
        self.assertEqual(
            "multi_objective_v1_executed_with_v0_baseline", comparison["mode"]
        )
        self.assertEqual(6, len({
            item["vehicle_id"] for item in result["assignments"]
        }))
        self.assertGreaterEqual(
            comparison["ab_metrics"]["normalized_cost_improvement"], 0.0
        )
        cycle = result["closed_loop_cycle"]
        self.assertEqual("executed", result["closed_loop_coordination"])
        self.assertEqual("openpit.closed-loop-cycle.v1", cycle["schema_version"])
        self.assertEqual("SUCCEEDED", cycle["status"])
        self.assertEqual("APPROVED", cycle["stage_results"]["safety"]["status"])
        self.assertEqual(2, cycle["revision_after"])
        self.assertTrue(all(
            item.get("status") == "completed"
            for item in cycle["next_state"]["tasks"]
        ))
        self.assertEqual(
            {item["task_id"]: item["vehicle_id"]
             for item in result["assignments"]},
            {item["task_id"]: item["vehicle_id"]
             for item in cycle["stage_results"]["scheduling"]["assignments"]},
        )

    def test_multiobjective_cycle_persists_and_passes_database_validation(self):
        result = run_structural_scenario(
            "s01", load_config(ROOT / "configs" / "s01_normal_6v.json"),
            seed=202607, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(
                Path(directory) / "artifacts" / "runs", "s01-cycle-db-test"
            )
            try:
                result["run_id"] = recorder.run_id
                recorder.record_policy_comparison(result)
                recorder.record_execution_feedback(result)
                self.assertEqual(1, recorder.record_closed_loop_cycle(result))
                recorder.record_structural_events(result)
                recorder.store.update_from_summary(recorder.run_id, result)
                validation = recorder.store.validate_closed_loop_evidence(
                    recorder.run_id, "s01", result
                )
                cycle_count = recorder.store.connection.execute(
                    "SELECT count(*) FROM closed_loop_cycles WHERE run_id=?",
                    (recorder.run_id,),
                ).fetchone()[0]
            finally:
                recorder.close()
        self.assertEqual(1, cycle_count)
        self.assertEqual("EVIDENCE_PASS", validation["status"])
        self.assertIn(
            "db_closed_loop_cycle_present",
            [item["check"] for item in validation["checks"]],
        )

    def test_v3_database_cycle_exports_as_direct_training_transition(self):
        result = run_structural_scenario(
            "s01", load_config(ROOT / "configs" / "s01_normal_6v.json"),
            seed=202607, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = EvidenceRecorder(
                root / "artifacts" / "runs", "s01-cycle-export-test",
            )
            try:
                result["run_id"] = recorder.run_id
                self.assertEqual(1, recorder.record_closed_loop_cycle(result))
                summary = dict(result)
                summary["scenario_seed"] = result["seed"]
                summary["scenario_mode"] = "seeded_random_map"
                recorder.store.update_from_summary(recorder.run_id, summary)
            finally:
                recorder.close()
            manifest = export_closed_loop_transition_dataset(
                root / "data" / "database" / "openpit.db",
                root / "datasets", "cycle-export-test",
                scenario_keys=["s01"], run_ids=[result["run_id"]],
            )
            record = json.loads(
                Path(manifest["transitions_path"]).read_text(
                    encoding="utf-8"
                ).splitlines()[0]
            )
        self.assertEqual("DATASET_QUALITY_PASS", manifest["dataset_quality"]["status"])
        self.assertEqual(1, manifest["record_count"])
        self.assertEqual("openpit.world-state.v1", record["state"]["schema_version"])
        self.assertEqual("openpit.decision-action.v1", record["action"]["schema_version"])
        self.assertEqual("dispatch_tasks", record["action"]["action_type"])
        self.assertEqual("openpit.world-state.v1", record["next_state"]["schema_version"])
        self.assertIsNone(record["reward"])
        self.assertEqual("NOT_AVAILABLE_NO_REWARD_MODEL", record["reward_status"])
        self.assertTrue(record["done"])
        self.assertFalse(record["data_quality"]["eligible_for_ppo_training"])
        readiness = manifest["learning_readiness"]
        self.assertEqual("STRUCTURAL_DATA_ONLY", readiness["collection_status"])
        self.assertEqual(1, readiness["successful_cycle_record_count"])
        self.assertEqual(0, readiness["physical_record_count"])
        self.assertEqual("DISABLED_BY_DESIGN", readiness["automatic_policy_update"])

    def test_auto_policy_runs_all_scenarios_with_direct_cycles(self):
        completed = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "run_scenario.py"),
                "--scenario", "all", "--runs", "1", "--seed", "202608",
                "--vehicle-count", "6", "--policy", "auto", "--no-record",
            ],
            cwd=str(ROOT), check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        summary = json.loads(completed.stdout)
        self.assertEqual("PASS", summary["status"])
        self.assertEqual(8, summary["closed_loop_pass_count"])
        self.assertEqual("auto", summary["requested_execution_policy"])
        policies = {
            item["scenario_key"]: item["resolved_execution_policy"]
            for item in summary["runs"]
        }
        self.assertEqual("multi-objective", policies["s01"])
        self.assertEqual("multi-objective", policies["s02"])
        self.assertTrue(all(
            policies[key] == "heuristic"
            for key in ("s03", "s04", "s05", "s06", "s07", "s09")
        ))

    def test_unified_cli_attaches_fixed_monitoring_contract(self):
        completed = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "run_scenario.py"),
                "--scenario", "s01", "--runs", "1", "--seed", "202601",
                "--vehicle-count", "6", "--policy", "auto", "--no-record",
                "--random-map",
            ],
            cwd=str(ROOT), check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(8, result["monitoring"]["fixed_station_count"])
        self.assertEqual(48, result["monitoring"]["fixed_observation_count"])
        self.assertTrue(result["monitoring"]["monitoring_synthetic_data"])
        self.assertEqual(48, len(result["monitoring_observations"]))
        self.assertEqual(
            8,
            len(result["world_state"]["environment"][
                "fixed_monitoring_stations"
            ]),
        )

    def test_policy_ab_entry_pairs_same_seed_and_both_direct_cycles(self):
        completed = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "run_scenario.py"),
                "--scenario", "s01", "--compare-policies", "--runs", "1",
                "--seed", "202711", "--vehicle-count", "6", "--no-record",
            ],
            cwd=str(ROOT), check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        summary = json.loads(completed.stdout)
        comparison = summary["policy_ab_comparison"]
        self.assertEqual("PASS", summary["status"])
        self.assertEqual("paired-a-b", summary["requested_execution_policy"])
        self.assertEqual(2, summary["run_count"])
        self.assertEqual("A_B_PASS", comparison["status"])
        self.assertEqual(1, comparison["complete_pair_count"])
        self.assertEqual(1, comparison["passed_pair_count"])
        pair = comparison["pairs"][0]
        self.assertEqual(202711, pair["seed"])
        self.assertTrue(pair["both_closed_loops_passed"])
        self.assertTrue(pair["both_safety_gates_passed"])

    def test_multi_objective_execution_scope_is_explicit(self):
        config = load_config(ROOT / "configs" / "s07_road_closure_6v.json")
        with self.assertRaisesRegex(ValueError, "random-map S01/S02 only"):
            run_structural_scenario(
                "s07", config, seed=202607, random_map=True,
                execution_policy="multi-objective",
            )

    def test_random_s02_can_execute_multi_objective_takeover(self):
        config = load_config(ROOT / "configs" / "s02_vehicle_failure_6v.json")
        result = run_structural_scenario(
            "s02", config, seed=202607, random_map=True, vehicle_count=6,
            execution_policy="multi-objective",
        )
        comparison = result["policy_comparison"]
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual("multi-objective-cost-v1", comparison["executed_policy"])
        self.assertEqual(
            "heuristic-route-load-global-unique-v0",
            comparison["initial_assignment_policy"],
        )
        self.assertTrue(result["released_task_ids"])
        self.assertTrue(all(
            item["vehicle_id"] != result["failed_vehicle_id"]
            for item in result["assignments"]
        ))
        self.assertGreaterEqual(
            comparison["ab_metrics"]["normalized_cost_improvement"], 0.0
        )
        cycle = result["closed_loop_cycle"]
        self.assertEqual("executed", result["closed_loop_coordination"])
        self.assertEqual("SUCCEEDED", cycle["status"])
        self.assertEqual(
            "vehicle_failure",
            cycle["stage_results"]["risk"]["event_type"],
        )
        self.assertEqual("APPROVED", cycle["stage_results"]["safety"]["status"])
        self.assertEqual(2, cycle["revision_after"])
        self.assertEqual(
            {item["task_id"]: item["vehicle_id"]
             for item in result["assignments"]},
            {item["task_id"]: item["vehicle_id"]
             for item in cycle["stage_results"]["scheduling"]["assignments"]},
        )

    def test_random_s05_records_selective_weather_response_and_dataset(self):
        config = load_config(ROOT / "configs" / "s05_extreme_weather_6v.json")
        result = run_structural_scenario(
            "s05", config, seed=202605, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual(
            "PARAMETERIZED_SYNTHETIC_SCENARIO",
            result["weather_event"]["data_origin"],
        )
        self.assertGreater(result["affected_task_count"], 0)
        self.assertGreater(result["unaffected_task_count"], 0)
        self.assertEqual(
            result["affected_task_count"], len(result["weather_decisions"])
        )
        self.assertTrue(all(
            item["measurement_status"] == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            and item["estimated_delay_s"] >= 0
            for item in result["weather_decisions"]
        ))

        result["run_id"] = "run-s05-dataset-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        records = build_structural_transition_dataset(result)
        self.assertEqual(result["affected_task_count"], len(records))
        self.assertTrue(all(
            item["state"]["trigger"]["event_type"]
            == "extreme_rainfall_road_capacity_degradation"
            for item in records
        ))
        self.assertTrue(all(
            item["action"]["eta"]["measurement_status"]
            == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            for item in records
        ))

    def test_random_s03_switches_failed_equipment_work_point(self):
        config = load_config(
            ROOT / "configs" / "s03_loading_equipment_failure_6v.json"
        )
        result = run_structural_scenario(
            "s03", config, seed=202603, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual(1, result["affected_task_count"])
        self.assertEqual(5, result["unaffected_task_count"])
        decision = result["equipment_decisions"][0]
        self.assertNotEqual(
            decision["failed_work_point_id"],
            decision["alternative_work_point_id"],
        )
        self.assertTrue(
            decision["constraint_results"]["alternative_route_reachable"]
        )
        self.assertEqual(
            "SURROGATE_ONLY_NOT_CARLA_MEASURED",
            decision["measurement_status"],
        )

        result["run_id"] = "run-s03-dataset-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        records = build_structural_transition_dataset(result)
        self.assertEqual(1, len(records))
        self.assertEqual(
            "loading_equipment_failure",
            records[0]["state"]["trigger"]["event_type"],
        )
        self.assertEqual(
            decision["alternative_work_point_id"],
            records[0]["action"]["route"]["alternative_work_point_id"],
        )

    def test_random_s04_controls_blast_edge_without_task_takeover(self):
        config = load_config(ROOT / "configs" / "s04_blasting_control_6v.json")
        result = run_structural_scenario(
            "s04", config, seed=202604, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertGreater(result["affected_task_count"], 0)
        self.assertGreater(result["unaffected_task_count"], 0)
        self.assertEqual(0, result["takeover_count"])
        self.assertEqual(0, result["safe_detour_count"])
        self.assertEqual(
            result["affected_task_count"],
            result["wait_for_clearance_count"],
        )
        for decision in result["blast_decisions"]:
            self.assertEqual(
                "hold_until_blast_clearance", decision["action_type"]
            )
            self.assertEqual(
                result["blast_event"]["clearance_tick"],
                decision["wait_until_tick"],
            )
            self.assertEqual(
                decision["vehicle_id"], decision["original_vehicle_id"]
            )

        p6_wait_result = run_structural_scenario(
            "s04", config, seed=202604, random_map=True, vehicle_count=6,
            eligible_pairs=physical_pairs_for_fake_carla(config),
            minimum_length_m=100.0, maximum_length_m=1000.0,
        )
        self.assertEqual("PASS", p6_wait_result["status"])
        self.assertGreater(p6_wait_result["wait_for_clearance_count"], 0)
        self.assertEqual(0, p6_wait_result["safe_detour_count"])

        wait_result = run_structural_scenario(
            # This seed is intentionally a distinct admitted layout that has
            # no acceptable bypass for the selected temporary-control edge.
            # It exercises the clearance-wait branch after P4 proximity
            # avoidance became part of common fleet admission.
            "s04", config, seed=202600, random_map=True, vehicle_count=6
        )
        self.assertGreater(wait_result["wait_for_clearance_count"], 0)
        self.assertTrue(all(
            item["vehicle_id"] == item["original_vehicle_id"]
            for item in wait_result["blast_decisions"]
            if item["action_type"] == "hold_until_blast_clearance"
        ))

        result["run_id"] = "run-s04-dataset-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        records = build_structural_transition_dataset(result)
        self.assertEqual(result["affected_task_count"], len(records))
        self.assertTrue(all(
            item["state"]["trigger"]["event_type"]
            == "planned_blasting_temporary_control" for item in records
        ))

    def test_random_s09_applies_two_events_to_one_state(self):
        config = load_config(
            ROOT / "configs" / "s09_compound_road_fault_6v.json"
        )
        result = run_structural_scenario(
            "s09", config, seed=202609, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertEqual(202609, result["seed"])
        self.assertEqual("COMPOUND_FEASIBLE", result[
            "scenario_admission_status"])
        self.assertEqual(
            ["road_closure", "vehicle_failure"],
            [item["event_type"] for item in result["compound_events"]],
        )
        self.assertLess(
            result["compound_events"][0]["tick"],
            result["compound_events"][1]["tick"],
        )
        self.assertEqual(1, result["reassignment_count"])
        decision = result["compound_failure_decisions"][0]
        self.assertNotEqual(
            result["failed_vehicle_id"], decision["selected_vehicle_id"]
        )
        self.assertNotIn(
            result["closed_edge_id"], decision["route_edge_ids"]
        )
        self.assertTrue(all(decision["constraint_results"].values()))
        displaced = decision.get(
            "displaced_task_recovery_route_contract"
        )
        if decision.get("selected_vehicle_current_task_id"):
            self.assertIsNone(displaced)
            self.assertEqual(
                decision["goal_point_id"],
                decision["displaced_task_goal_point_id"],
            )
            self.assertTrue(decision["constraint_results"][
                "displaced_task_recovery_reachable"
            ])
            self.assertTrue(decision["constraint_results"][
                "candidate_route_aligned_with_failed_goal"
            ])
            self.assertEqual(
                decision["displaced_task_recovery_distance_m"], 0.0
            )
        failed_states = [
            item for item in result["final_vehicle_states"]
            if item["vehicle_id"] == result["failed_vehicle_id"]
        ]
        self.assertEqual(1, len(failed_states))
        self.assertEqual("fault", failed_states[0]["health"])
        self.assertFalse(failed_states[0]["available"])
        self.assertEqual("failed_isolated", failed_states[0]["task_status"])

        retried = run_structural_scenario(
            "s09", config, seed=202630, random_map=True, vehicle_count=6
        )
        self.assertEqual(202630, retried["seed"])
        self.assertGreaterEqual(retried["generation_attempt"], 0)
        self.assertEqual(202630 + retried["generation_attempt"],
                         retried["workload_seed"])
        self.assertEqual("CLOSED_LOOP_PASS", retried["closed_loop_status"])

        result["run_id"] = "run-s09-dataset-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        records = build_structural_transition_dataset(result)
        self.assertEqual(
            result["road_affected_task_count"] + result["reassignment_count"],
            len(records),
        )
        self.assertEqual(
            {1, 2}, {item["action"]["compound_stage"] for item in records}
        )
        self.assertTrue(all(
            item["state"]["trigger"]["event_type"]
            == "compound_road_closure_then_vehicle_failure"
            for item in records
        ))

    def test_random_s07_closes_real_edge_and_replans_only_affected_tasks(self):
        config = load_config(ROOT / "configs" / "s07_road_closure_6v.json")
        result = run_structural_scenario(
            "s07", config, seed=202601, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["closed_edge_id"].startswith("carla-topology-edge:"))
        self.assertGreater(result["affected_task_count"], 0)
        self.assertGreater(result["unaffected_task_count"], 0)
        self.assertEqual(
            result["affected_task_count"], result["same_vehicle_replan_count"]
        )
        self.assertEqual(0, result["takeover_count"])
        for change in result["route_changes"]:
            self.assertNotIn(
                result["closed_edge_id"], change["replanned_edge_ids"]
            )
            self.assertEqual("route_replan_same_vehicle", change["action_type"])

    def test_random_s06_schedules_shared_road_with_capacity_and_headway(self):
        config = load_config(ROOT / "configs" / "s06_congestion_6v.json")
        result = run_structural_scenario(
            "s06", config, seed=202606, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("CLOSED_LOOP_PASS", result["closed_loop_status"])
        self.assertTrue(result["bottleneck_edge_id"].startswith(
            "carla-topology-edge:"
        ))
        self.assertGreaterEqual(result["affected_task_count"], 2)
        self.assertGreater(result["unaffected_task_count"], 0)
        self.assertEqual(
            result["affected_task_count"], len(result["traffic_decisions"])
        )
        headway = result["congestion_event"]["minimum_safety_headway_seconds"]
        for previous, current in zip(
                result["traffic_decisions"], result["traffic_decisions"][1:]):
            self.assertGreaterEqual(
                current["scheduled_entry_s"] + 1e-6,
                previous["scheduled_exit_s"] + headway,
            )
        self.assertTrue(all(
            item["measurement_status"] == "SURROGATE_ONLY_NOT_CARLA_MEASURED"
            for item in result["traffic_decisions"]
        ))

        result["run_id"] = "run-s06-dataset-test"
        result["database_evidence_validation"] = {"status": "EVIDENCE_PASS"}
        records = build_structural_transition_dataset(result)
        self.assertEqual(result["affected_task_count"], len(records))
        self.assertTrue(all(
            item["state"]["trigger"]["event_type"]
            == "shared_road_capacity_degradation" for item in records
        ))
        self.assertTrue(all(
            item["action"]["traffic"]["measurement_status"]
            == "SURROGATE_ONLY_NOT_CARLA_MEASURED" for item in records
        ))

    def test_random_s07_uses_takeover_only_when_original_has_no_safe_bypass(self):
        config = load_config(ROOT / "configs" / "s07_road_closure_6v.json")
        result = run_structural_scenario(
            "s07", config, seed=202608, random_map=True, vehicle_count=6
        )
        self.assertEqual("PASS", result["status"])
        self.assertGreater(result["takeover_count"], 0)
        self.assertEqual(result["takeover_count"], len(result["takeover_assignments"]))
        for change in result["route_changes"]:
            if not change["takeover_required"]:
                continue
            self.assertEqual(
                "task_takeover_after_no_safe_bypass", change["action_type"]
            )
            self.assertNotEqual(change["original_vehicle_id"], change["vehicle_id"])
            self.assertTrue(change["candidate_evaluations"])
            self.assertNotIn(result["closed_edge_id"], change["replanned_edge_ids"])


if __name__ == "__main__":
    unittest.main()
