import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.monitoring import (
    build_fixed_observations,
    build_mobile_observations,
    load_monitoring_layout,
    monitoring_summary,
)
from open_pit_agent.config import load_config
from open_pit_agent.adapters.mock_adapter import MockAdapter
from open_pit_agent.risk import load_risk_scenario


class FixedMonitoringTest(unittest.TestCase):
    def setUp(self):
        self.layout = load_monitoring_layout(
            PROJECT_ROOT / "configs" / "monitoring_demo.json"
        )
        self.risk = load_risk_scenario(
            PROJECT_ROOT
            / "configs"
            / "risk_slope_competition_synthetic.json"
        )

    def test_layout_has_four_areas_and_eight_fixed_stations(self):
        self.assertEqual(4, len(self.layout.areas))
        self.assertEqual(8, len(self.layout.stations))
        self.assertEqual(
            {item.area_id for item in self.layout.areas},
            {item.area_id for item in self.layout.stations},
        )

    def test_builds_one_observation_per_station_and_sample(self):
        observations = build_fixed_observations(
            self.layout, self.risk.observations
        )

        self.assertEqual(32, len(observations))
        self.assertTrue(
            all(item.source_type == "fixed_station" for item in observations)
        )
        self.assertTrue(
            all(item.quality["status"] == "valid" for item in observations)
        )
        self.assertTrue(
            all(item.quality["completeness"] == 1.0 for item in observations)
        )

    def test_existing_fixed_risk_values_feed_raw_station_records(self):
        observations = build_fixed_observations(
            self.layout, self.risk.observations
        )
        by_station_and_tick = {
            (item.source_id, item.tick): item for item in observations
        }

        gnss = by_station_and_tick[("gnss_station_01", 520)]
        pore = by_station_and_tick[("pore_pressure_station_01", 520)]
        weather = by_station_and_tick[("weather_station_01", 520)]
        self.assertEqual(90.0, gnss.metrics["fixed_displacement_mm"])
        self.assertEqual(95.0, pore.metrics["pore_pressure_kpa"])
        self.assertEqual(130.0, weather.metrics["rainfall_mm_24h"])
        self.assertNotIn("mobile_displacement_mm", gnss.metrics)

    def test_summary_exposes_counts_and_artifact_name(self):
        observations = build_fixed_observations(
            self.layout, self.risk.observations
        )
        summary = monitoring_summary(self.layout, observations)

        self.assertEqual(4, summary["monitoring_area_count"])
        self.assertEqual(8, summary["fixed_station_count"])
        self.assertEqual(32, summary["fixed_observation_count"])
        self.assertEqual(
            "monitoring_observations.jsonl",
            summary["monitoring_artifact"],
        )

    def test_three_mine_trucks_build_mobile_observations(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = MockAdapter(config)
        adapter.connect()
        try:
            observations = build_mobile_observations(
                self.layout,
                adapter.list_states(),
                520,
                risk_observation=self.risk.observations[-1],
                telemetry_source="mock_adapter",
            )
        finally:
            adapter.close()

        self.assertEqual(3, len(observations))
        self.assertEqual(
            3, len({item.source_id for item in observations})
        )
        self.assertTrue(
            all(
                item.source_type == "mobile_equipment"
                for item in observations
            )
        )
        slope_truck = next(
            item
            for item in observations
            if item.source_id == "inspection_vehicle_02"
        )
        self.assertEqual(
            88.0,
            slope_truck.metrics["mobile_displacement_mm"],
        )
        self.assertEqual(
            "mock_adapter",
            slope_truck.quality["telemetry_source"],
        )
        self.assertEqual(
            "synthetic_demo",
            slope_truck.quality["sensor_values_source"],
        )


if __name__ == "__main__":
    unittest.main()
