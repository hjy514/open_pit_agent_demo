import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.dashboard import (
    DashboardDataError,
    RunRepository,
    project_run_state,
)


class DashboardProjectionTest(unittest.TestCase):
    def test_live_events_project_tasks_orders_and_risk(self):
        events = [
            {
                "event_type": "carla_connected",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "vehicles": [
                        {
                            "vehicle_id": "vehicle-01",
                            "health": "healthy",
                            "position": {"x": 1, "y": 2},
                        }
                    ]
                },
            },
            {
                "event_type": "initial_schedule",
                "timestamp": "2026-08-01T00:00:01Z",
                "payload": {
                    "assignments": [
                        {
                            "task_id": "task-01",
                            "zone_id": "zone-01",
                            "vehicle_id": "vehicle-01",
                        }
                    ]
                },
            },
            {
                "event_type": "runtime_scene",
                "timestamp": "2026-08-01T00:00:01Z",
                "payload": {
                    "map_name": "Town03",
                    "zones": [
                        {
                            "zone_id": "zone-01",
                            "position": {"x": 10, "y": 20},
                        }
                    ],
                },
            },
            {
                "event_type": "risk_assessed",
                "timestamp": "2026-08-01T00:00:02Z",
                "payload": {
                    "assessment_id": "assessment-080",
                    "tick": 80,
                    "sample_id": "sample-080",
                    "zone_id": "risk-zone",
                    "level": "orange",
                    "trend": "rising",
                    "reasons": ["threshold_reached"],
                },
            },
            {
                "event_type": "risk_guidance_generated",
                "timestamp": "2026-08-01T00:00:02Z",
                "payload": {
                    "assessment_id": "assessment-080",
                    "impacts": ["risk impact"],
                    "prevention_measures": ["prevention"],
                },
            },
            {
                "event_type": "carla_tick",
                "timestamp": "2026-08-01T00:00:04Z",
                "payload": {
                    "tick": 85,
                    "vehicles": [
                        {
                            "vehicle_id": "vehicle-01",
                            "health": "healthy",
                            "position": {"x": 3, "y": 4},
                        }
                    ],
                },
            },
            {
                "event_type": "work_order_created",
                "timestamp": "2026-08-01T00:00:03Z",
                "payload": {
                    "work_order_id": "wo-task-02",
                    "task_id": "task-02",
                    "status": "pending",
                },
            },
            {
                "event_type": "work_order_status_changed",
                "timestamp": "2026-08-01T00:00:04Z",
                "payload": {
                    "work_order_id": "wo-task-02",
                    "task_id": "task-02",
                    "tick": 81,
                    "to_status": "assigned",
                },
            },
            {
                "event_type": "task_completed",
                "timestamp": "2026-08-01T00:00:05Z",
                "payload": {
                    "task_id": "task-01",
                    "vehicle_id": "vehicle-01",
                    "tick": 90,
                },
            },
        ]

        state = project_run_state("live-run", events)

        self.assertEqual("running", state["phase"])
        self.assertEqual("orange", state["current_risk"]["level"])
        self.assertEqual(
            ["risk impact"],
            state["current_risk"]["guidance"]["impacts"],
        )
        self.assertEqual(90, state["latest_tick"])
        self.assertEqual(
            "completed", state["tasks"][0]["status"]
        )
        self.assertEqual(
            "assigned", state["work_orders"][0]["status"]
        )
        self.assertEqual(
            1, state["metrics"]["completed_task_count"]
        )
        self.assertEqual("zone-01", state["zones"][0]["zone_id"])
        self.assertEqual(
            1, len(state["vehicle_trails"]["vehicle-01"])
        )

    def test_repository_rejects_unknown_run_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "known-run"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event_type": "instruction_received",
                        "payload": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            repository = RunRepository(root)

            with self.assertRaises(DashboardDataError):
                repository.resolve_run("../known-run")


if __name__ == "__main__":
    unittest.main()
