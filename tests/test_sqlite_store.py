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
                    "vehicle_id": "inspection_vehicle_02",
                    "task_id": "initial:secondary_patrol_zone_02",
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
                3,
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


if __name__ == "__main__":
    unittest.main()
