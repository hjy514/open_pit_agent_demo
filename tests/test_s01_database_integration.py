import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.config import load_config
from open_pit_agent.scenario import build_episode
from open_pit_agent.sqlite_store import SqliteRunStore


class S01DatabaseIntegrationTest(unittest.TestCase):
    def test_six_vehicle_episode_is_persisted(self):
        config = load_config(PROJECT_ROOT / "configs" / "s01_normal_6v.json")
        episode = build_episode(config, run_id="s01-db-test", seed=202601)
        with tempfile.TemporaryDirectory() as td:
            store = SqliteRunStore(Path(td) / "openpit.db")
            try:
                store.record_episode(
                    episode,
                    config_path=PROJECT_ROOT / "configs" / "s01_normal_6v.json",
                    policy_version="rule-baseline-v1",
                )
                run = store.connection.execute(
                    "SELECT scenario_id, scenario_seed, fleet_size FROM scenario_runs WHERE run_id=?",
                    (episode.run_id,),
                ).fetchone()
                self.assertEqual(("s01-normal-6v", 202601, 6), tuple(run))
                vehicle_count = store.connection.execute(
                    "SELECT count(*) FROM run_vehicles WHERE run_id=?",
                    (episode.run_id,),
                ).fetchone()[0]
                self.assertEqual(6, vehicle_count)
                task_count = store.connection.execute(
                    "SELECT count(*) FROM episode_tasks WHERE run_id=?",
                    (episode.run_id,),
                ).fetchone()[0]
                self.assertEqual(6, task_count)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
