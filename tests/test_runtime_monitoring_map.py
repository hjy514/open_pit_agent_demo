import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.runtime_state import RuntimeState


class RuntimeMonitoringMapTest(unittest.TestCase):
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
