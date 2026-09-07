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
from open_pit_agent.decision_intelligence import (
    aggregate_structural_transition_datasets, build_structural_transition_dataset,
    load_structural_reward_config, write_structural_transition_dataset,
    export_closed_loop_transition_dataset,
)
from open_pit_agent.scenario import (
    SCENARIO_CATALOG, run_structural_scenario, summarize_structural_batch,
    validate_structural_closed_loop,
)
from open_pit_agent.scenario.carla_execution import (
    run_carla_scenario_execution, run_s01_carla_execution,
)
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


class UnifiedScenarioRunnerTests(unittest.TestCase):
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
        self.assertIn("s08边坡失稳Golden Demo", completed.stdout)
        self.assertIn("s01–s07/s09统一多车事件执行", completed.stdout)
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
        self.assertTrue(FakeExecutionAdapter.last_instance.destroyed)
        self.assertTrue(FakeExecutionAdapter.last_instance.closed)

    def test_s02_carla_bridge_applies_fault_and_task_takeover(self):
        class FakeEventAdapter:
            last_instance = None

            def __init__(self, config, load_map=False):
                self.config = config
                self.tasks = []
                self.tick_count = 0
                self.faults = []
                self.reassignments = []
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
        result = run_carla_scenario_execution(
            "s02", config,
            seed=202601, vehicle_count=6, ticks=9,
            physical_route_pairs=planner_pairs_for_fake_carla(config),
            adapter_factory=FakeEventAdapter,
        )
        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["scenario_event_applied"])
        self.assertEqual(1, len(FakeEventAdapter.last_instance.faults))
        self.assertEqual(1, len(FakeEventAdapter.last_instance.reassignments))
        self.assertEqual("SUCCEEDED", result["closed_loop_cycle"]["status"])

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
            "s07", config, seed=202607, random_map=True, vehicle_count=6
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
        for decision in result["blast_decisions"]:
            self.assertIn(decision["action_type"], {
                "blast_zone_safe_route", "hold_until_blast_clearance",
            })
            if decision["action_type"] == "blast_zone_safe_route":
                self.assertNotIn(
                    result["restricted_edge_id"],
                    decision["replanned_edge_ids"],
                )

        wait_result = run_structural_scenario(
            "s04", config, seed=202605, random_map=True, vehicle_count=6
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
        failed_states = [
            item for item in result["final_vehicle_states"]
            if item["vehicle_id"] == result["failed_vehicle_id"]
        ]
        self.assertEqual(1, len(failed_states))
        self.assertEqual("fault", failed_states[0]["health"])
        self.assertFalse(failed_states[0]["available"])
        self.assertEqual("failed_isolated", failed_states[0]["task_status"])

        retried = run_structural_scenario(
            "s09", config, seed=202614, random_map=True, vehicle_count=6
        )
        self.assertEqual(202614, retried["seed"])
        self.assertGreater(retried["generation_attempt"], 0)
        self.assertEqual(202614 + retried["generation_attempt"],
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
            "s07", config, seed=202607, random_map=True, vehicle_count=6
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
