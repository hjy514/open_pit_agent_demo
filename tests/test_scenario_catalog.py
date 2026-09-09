import json
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.scenario.catalog import (
    DEFAULT_CATALOG_PATH, compatibility_config_path, load_scenario_catalog,
    scenario_spec, selectable_vehicle_counts, validate_scenario_request,
)


class ScenarioCatalogTests(unittest.TestCase):
    def test_catalog_is_valid_and_has_all_expected_scenarios(self):
        catalog = load_scenario_catalog()
        self.assertEqual(
            {"s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09"},
            set(catalog),
        )
        self.assertEqual("openpit.scenario-catalog.v1", json.loads(
            DEFAULT_CATALOG_PATH.read_text(encoding="utf-8")
        )["schema_version"])

    def test_legacy_config_paths_are_resolved_from_catalog(self):
        self.assertTrue(compatibility_config_path("s01").is_file())
        self.assertTrue(compatibility_config_path("s08").is_file())
        self.assertEqual((6, 8), selectable_vehicle_counts("s02"))

    def test_every_catalog_entry_exposes_the_same_scenario_spec_contract(self):
        for key in load_scenario_catalog():
            spec = scenario_spec(key).to_dict()
            self.assertEqual("openpit.scenario-spec.v1", spec["schema_version"])
            self.assertEqual(key, spec["scenario_key"])
            self.assertIn("fleet", spec)
            self.assertIn("events", spec)
            self.assertIn("success_criteria", spec)
        self.assertEqual(
            "legacy_golden_compatibility_adapter",
            scenario_spec("s08").implementation_mode,
        )

    def test_request_validation_is_fail_closed(self):
        validate_scenario_request("s01", "structural", 6)
        validate_scenario_request("s08", "carla", 3)
        with self.assertRaises(ValueError):
            validate_scenario_request("s08", "structural", 3)
        with self.assertRaises(ValueError):
            validate_scenario_request("s02", "carla", 3)
