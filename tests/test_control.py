import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.control import (
    ControlError,
    DemoControlManager,
    UnifiedScenarioControlManager,
)


class DemoControlTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DemoControlManager(
            project_root=PROJECT_ROOT,
            artifacts_root=Path(self.temp_dir.name) / "runs",
            python_executable="/safe/python",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_red_scenario_builds_fixed_argument_list(self):
        command = self.manager.command_for("red_response")

        self.assertEqual("/safe/python", command[0])
        self.assertEqual(
            str(PROJECT_ROOT / "run_demo.py"), command[1]
        )
        self.assertIn("--load-map", command)
        self.assertIn("--spawn-missing", command)
        self.assertIn("--risk-config", command)
        self.assertIn(
            str(
                PROJECT_ROOT
                / "configs"
                / "risk_slope_red_synthetic.json"
            ),
            command,
        )
        self.assertNotIn("--inject-failure", command)

    def test_unknown_scenario_cannot_build_command(self):
        with self.assertRaises(ControlError):
            self.manager.command_for("../../arbitrary")

    def test_only_allowlisted_fault_scenarios_inject_failure(self):
        fault = self.manager.command_for("fault_recovery")
        competition = self.manager.command_for(
            "competition_demo"
        )
        normal = self.manager.command_for(
            "normal_inspection"
        )

        self.assertIn("--inject-failure", fault)
        self.assertIn("--inject-failure", competition)
        self.assertIn("--risk-config", competition)
        mine_competition = self.manager.command_for(
            "mine_competition_demo"
        )
        self.assertIn("--monitoring-config", mine_competition)
        self.assertIn(
            str(PROJECT_ROOT / "configs" / "monitoring_demo.json"),
            mine_competition,
        )
        ticks_index = competition.index("--ticks")
        self.assertEqual("6000", competition[ticks_index + 1])
        self.assertIn(
            str(
                PROJECT_ROOT
                / "configs"
                / "risk_slope_competition_synthetic.json"
            ),
            competition,
        )
        self.assertNotIn("--inject-failure", normal)


class UnifiedScenarioControlTest(unittest.TestCase):
    def setUp(self):
        self.manager = UnifiedScenarioControlManager(PROJECT_ROOT)

    def tearDown(self):
        self.manager.close()

    def test_catalog_exposes_all_scenarios_with_ui_options(self):
        scenarios = self.manager.scenarios()
        self.assertEqual(
            ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09"],
            [item["scenario_id"] for item in scenarios],
        )
        s08 = next(item for item in scenarios if item["scenario_id"] == "s08")
        self.assertEqual(["carla"], s08["modes"])
        self.assertEqual([3], s08["vehicle_counts"])

    def test_command_uses_single_public_scenario_entry(self):
        command = self.manager.command_for({
            "scenario_id": "s02", "mode": "structural",
            "vehicle_count": 6, "seed": 202601, "policy": "auto",
        })
        self.assertEqual(str(PROJECT_ROOT / "run_scenario.sh"), command[0])
        self.assertIn("s02", command)
        self.assertIn("202601", command)
        self.assertIn("--random-map", command)
        self.assertIn("--ui-sync", command)
        self.assertIn("--playback-delay-seconds", command)

    def test_s08_keeps_fixed_golden_adapter_behind_unified_entry(self):
        command = self.manager.command_for({
            "scenario_id": "s08", "mode": "carla",
            "vehicle_count": 3, "seed": 202616, "policy": "auto",
        })
        self.assertEqual([
            str(PROJECT_ROOT / "run_scenario.sh"),
            "--scenario", "s08", "--mode", "carla",
        ], command)

    def test_carla_command_uses_same_runtime_sync_contract(self):
        command = self.manager.command_for({
            "scenario_id": "s02", "mode": "carla",
            "vehicle_count": 6, "seed": 202601, "policy": "auto",
        })
        self.assertIn("--ui-sync", command)
        self.assertNotIn("--playback-delay-seconds", command)
        self.assertIn("--operator-review", command)
        self.assertIn("--load-map", command)

    def test_random_seed_is_resolved_once_and_exposed_for_replay(self):
        resolved = self.manager._validate_request({
            "scenario_id": "s01", "mode": "structural",
            "vehicle_count": 6, "seed": 1, "random_seed": True,
            "policy": "auto",
        })
        self.assertEqual("random_generated", resolved["seed_mode"])
        self.assertGreaterEqual(resolved["seed"], 0)
        self.assertLessEqual(resolved["seed"], 2147483647)

    def test_request_validation_rejects_unsupported_combination(self):
        with self.assertRaises(ControlError):
            self.manager.command_for({
                "scenario_id": "s08", "mode": "structural",
                "vehicle_count": 6, "seed": 1, "policy": "heuristic",
            })

    def test_final_result_is_extracted_from_mixed_process_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "scenario.log"
            log_path.write_text(
                "WARNING: CARLA message\n"
                "{\n"
                '  "status": "PARTIAL",\n'
                '  "scenario_key": "s04",\n'
                '  "run_id": "run-001",\n'
                '  "task_count": 6,\n'
                '  "completed_task_count": 4,\n'
                '  "closed_loop_validation": {"status": "FAILED"},\n'
                '  "route_evidence_admission": {"status": "P5_PENDING_CARLA_VALIDATION"},\n'
                '  "database_path": "/tmp/openpit.db"\n'
                "}\n",
                encoding="utf-8",
            )
            self.manager._log_path = log_path

            result = self.manager._read_result_summary()

        self.assertEqual("PARTIAL", result["status"])
        self.assertEqual("run-001", result["run_id"])
        self.assertEqual(4, result["completed_task_count"])
        self.assertEqual("FAILED", result["closed_loop_status"])
        self.assertEqual(
            "P5_PENDING_CARLA_VALIDATION",
            result["route_evidence_admission"],
        )

    def test_pause_and_resume_keep_the_same_process_and_request(self):
        class RunningProcess:
            pid = 1234

            @staticmethod
            def poll():
                return None

            @staticmethod
            def send_signal(_signal):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            self.manager._process = RunningProcess()
            self.manager._request = {
                "scenario_id": "s04", "mode": "structural",
                "vehicle_count": 6, "seed": 202601,
            }
            self.manager._control_path = Path(temp_dir) / "control.json"

            paused = self.manager.pause()
            pause_payload = self.manager._control_path.read_text(
                encoding="utf-8"
            )
            resumed = self.manager.resume()

        self.assertEqual("paused", paused["state"])
        self.assertEqual(1234, paused["pid"])
        self.assertIn('"requested_state": "paused"', pause_payload)
        self.assertEqual("running", resumed["state"])
        self.assertEqual(paused["request"], resumed["request"])

    def test_s08_golden_adapter_rejects_fake_resume_semantics(self):
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        self.manager._process = RunningProcess()
        self.manager._request = {"scenario_id": "s08"}
        with self.assertRaises(ControlError):
            self.manager.pause()
        self.manager._process = None

    def test_carla_startup_failure_is_not_reported_as_finished(self):
        self.manager._request = {
            "scenario_id": "s01", "mode": "carla",
            "vehicle_count": 6, "seed": 202601,
        }
        self.manager._carla_state = "timeout"
        self.manager._carla_error = "CARLA RPC timeout"

        status = self.manager.status()

        self.assertEqual("failed", status["state"])
        self.assertEqual("CARLA RPC timeout", status["carla"]["error"])

    def test_terminated_and_successful_processes_have_distinct_states(self):
        class FinishedProcess:
            pid = 1234

            def __init__(self, exit_code):
                self.exit_code = exit_code

            def poll(self):
                return self.exit_code

        self.manager._request = {
            "scenario_id": "s01", "mode": "structural",
            "vehicle_count": 6, "seed": 202601,
        }
        self.manager._process = FinishedProcess(0)
        self.assertEqual("completed", self.manager.status()["state"])

        self.manager._stop_requested = True
        self.assertEqual("terminated", self.manager.status()["state"])


if __name__ == "__main__":
    unittest.main()
