import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.runtime_state import RuntimeState


class RuntimeMonitoringMapTest(unittest.TestCase):
    def test_execution_feedback_updates_tasks_vehicles_and_revision(self):
        runtime = RuntimeState()
        runtime.sync_snapshot({
            "outcome": {"status": "RUNNING", "task_count": 2},
            "world_state": {
                "vehicles": [
                    {"vehicle_id": "truck-1", "status": "executing",
                     "position": {"x": 1, "y": 2, "z": 3}},
                    {"vehicle_id": "truck-2", "status": "idle"},
                ],
                "tasks": [
                    {"task_id": "task-1", "status": "executing"},
                    {"task_id": "task-2", "status": "pending"},
                ],
            },
            "execution_feedback": [{
                "schema_version": "openpit.execution-feedback.v1",
                "command_id": "command-1", "phase": "terminal",
                "status": "SUCCEEDED", "adapter_type": "MockAdapter",
                "physical_execution": False,
                "measurement_status": "STRUCTURAL_ONLY_NO_PHYSICS",
                "safety_gate_status": "APPROVED",
                "task_states": [
                    {"task_id": "task-1", "status": "completed"}
                ],
                "vehicle_states": [
                    {"vehicle_id": "truck-1", "status": "idle"}
                ],
                "events": [{"event_type": "task_completed",
                            "message": "task-1 completed"}],
                "timestamp": "2026-09-06T10:00:00Z",
            }],
        })

        state = runtime.get_state()
        self.assertEqual(1, state["state_revision"])
        self.assertEqual("completed", state["tasks"][0]["status"])
        self.assertEqual("pending", state["tasks"][1]["status"])
        self.assertEqual("idle", state["vehicles"][0]["status"])
        self.assertEqual("truck-2", state["vehicles"][1]["id"])
        self.assertEqual(
            {"x": 1.0, "y": 2.0, "z": 3.0},
            state["vehicles"][0]["position_xyz"],
        )
        self.assertEqual(
            "STRUCTURAL_ONLY_NO_PHYSICS",
            state["feedback"]["latest_execution_feedback"][
                "measurement_status"
            ],
        )
        self.assertEqual("RUNNING", state["feedback"]["status"])

        applied_again = runtime.apply_execution_feedback(
            state["execution_feedback_history"][0]
        )
        self.assertFalse(applied_again)
        self.assertEqual(1, runtime.state_revision)

        runtime.tasks[0]["status"] = "stale"
        runtime.apply_execution_feedback(state["execution_feedback_history"][0])
        self.assertEqual("completed", runtime.tasks[0]["status"])
        self.assertEqual(1, runtime.state_revision)

    def test_execution_feedback_rejects_unknown_schema(self):
        runtime = RuntimeState()
        with self.assertRaises(ValueError):
            runtime.apply_execution_feedback({
                "schema_version": "unknown.v1",
                "command_id": "command-1",
                "phase": "terminal",
                "status": "SUCCEEDED",
            })
        self.assertEqual(0, runtime.state_revision)

    def test_runtime_accepts_canonical_world_state_contract(self):
        runtime = RuntimeState()
        runtime.sync_snapshot({
            "run_context": {
                "run_id": "run-1", "scenario_key": "s02",
                "scenario_id": "s02-6v", "seed": 202602,
            },
            "execution": {
                "mode": "mock_structural", "simulator": "structural",
                "physical_execution": False, "status": "PASS",
            },
            "outcome": {
                "status": "PASS", "task_count": 1,
                "completed_task_count": 1, "all_tasks_completed": True,
            },
            "lifecycle": {
                "schema_version": "openpit.scenario-lifecycle.v1",
                "current_phase": "finish", "phases": [],
            },
            "world_state": {
                "run_id": "run-1",
                "vehicles": [{"vehicle_id": "truck-1", "health": "healthy"}],
                "tasks": [{"task_id": "task-1", "status": "completed"}],
                "roads": {"road-1": "OPEN"},
                "environment": {"map_context": {"map_id": "0325_5"}},
                "monitoring": {}, "risk": {},
                "traffic": {"density": 0.2},
                "equipment": {"loader-1": "AVAILABLE"},
            },
        })

        state = runtime.get_state()
        self.assertEqual("run-1", state["run_id"])
        self.assertEqual("s02", state["scenario"]["scenario_key"])
        self.assertEqual("structural", state["execution"]["simulator"])
        self.assertTrue(state["feedback"]["all_tasks_completed"])
        self.assertEqual({"road-1": "OPEN"}, state["roads"])
        self.assertEqual("truck-1", state["vehicles"][0]["id"])
        self.assertEqual("finish", state["lifecycle"]["current_phase"])

    def test_runtime_exposes_live_closed_loop_status(self):
        runtime = RuntimeState()
        runtime.sync_snapshot(
            {
                "monitoring": {
                    "phase": "任务调度",
                    "phase_index": 2,
                    "fixed_station_count": 8,
                    "mobile_equipment_count": 3,
                    "total_observation_count": 47,
                    "risk_level": "red",
                    "work_order_count": 4,
                    "closed_work_order_count": 0,
                }
            }
        )
        runtime.sync_snapshot(
            {
                "monitoring": {
                    "phase": "闭环完成",
                    "phase_index": 5,
                    "risk_level": "blue",
                    "previous_risk_level": "red",
                    "closed_work_order_count": 4,
                    "feedback_count": 4,
                    "closed_loop_complete": True,
                }
            }
        )

        state = runtime.get_monitoring()

        self.assertEqual(8, state["fixed_station_count"])
        self.assertEqual(3, state["mobile_equipment_count"])
        self.assertEqual(47, state["total_observation_count"])
        self.assertEqual("blue", state["risk_level"])
        self.assertEqual("red", state["previous_risk_level"])
        self.assertEqual(4, state["closed_work_order_count"])
        self.assertTrue(state["closed_loop_complete"])

    def test_map_state_exposes_monitoring_layers(self):
        runtime = RuntimeState()
        runtime.sync_snapshot(
            {
                "environment": {
                    "monitoring_areas": [
                        {
                            "area_id": "area-1",
                            "display_name": "监测区1",
                            "area_type": "slope",
                            "center_position": {
                                "x": 300.0,
                                "y": 200.0,
                                "z": 5.0,
                            },
                        }
                    ],
                    "fixed_monitoring_stations": [
                        {
                            "station_id": "station-1",
                            "display_name": "GNSS站1",
                            "area_id": "area-1",
                            "station_type": "gnss",
                            "position": {
                                "x": 305.0,
                                "y": 202.0,
                                "z": 5.0,
                            },
                            "online": True,
                        }
                    ],
                }
            }
        )

        state = runtime.get_map_state()

        self.assertEqual(1, len(state["monitoring_areas"]))
        self.assertEqual(1, len(state["fixed_monitoring_stations"]))
        self.assertGreater(state["bounds"]["max_x"], 300.0)


if __name__ == "__main__":
    unittest.main()
