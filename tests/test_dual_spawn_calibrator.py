import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from open_pit_agent.map_resources import MapResourceStore
from open_pit_agent.map_resources.dual_spawn_calibrator import destroy_actor_safely, ensure_no_existing_vehicles, select_static_candidates, verify_dual_spawn_pairs


class Actor:
    def __init__(self): self.destroyed = False
    def destroy(self): self.destroyed = True


class FakeMap:
    name = "/Game/Carla/Maps/0325_5"


class Blueprints:
    def find(self, blueprint): return blueprint


class World:
    def __init__(self): self.calls, self.actors = 0, []
    def get_map(self): return FakeMap()
    def get_blueprint_library(self): return Blueprints()
    def try_spawn_actor(self, blueprint, transform):
        self.calls += 1
        if self.calls == 2: return None
        actor = Actor(); self.actors.append(actor); return actor


class FailingWorld(World):
    def try_spawn_actor(self, blueprint, transform):
        raise RuntimeError("RPC connection lost")


class ExistingVehicle:
    id = 42


class ActorCollection:
    def filter(self, pattern):
        return [ExistingVehicle()]


class OccupiedWorld(World):
    def get_actors(self):
        return ActorCollection()


class FalseButRemovedActor:
    id = 99
    is_alive = False
    def destroy(self): return False


class ActorLookupWorld:
    def __init__(self, remaining=None): self.remaining = remaining
    def get_actor(self, actor_id): return self.remaining


class EmptyActorList:
    def filter(self, pattern): return []


class StaleLookupButEmptyListWorld(ActorLookupWorld):
    def get_actors(self): return EmptyActorList()


class DualSpawnTests(unittest.TestCase):
    def test_false_destroy_result_is_accepted_only_when_world_confirms_absence(self):
        actor = FalseButRemovedActor()
        self.assertTrue(destroy_actor_safely(ActorLookupWorld(), actor))
        self.assertTrue(
            destroy_actor_safely(
                StaleLookupButEmptyListWorld(remaining=actor), actor
            )
        )
        with self.assertRaisesRegex(RuntimeError, "still exists"):
            destroy_actor_safely(
                ActorLookupWorld(remaining=actor), actor,
                confirmation_timeout_seconds=0,
            )

    def test_existing_vehicle_world_is_rejected_before_calibration(self):
        with self.assertRaisesRegex(RuntimeError, "clean world"):
            ensure_no_existing_vehicles(OccupiedWorld())

    def test_static_candidates_and_blocked_dual_spawn_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                store.upsert_spawn_calibration_points("m", [
                    {"spawn_point_index": 0, "x": 0, "y": 0, "z": 0, "yaw": 0, "heavy_truck_allowed": 1, "validation_status": "VERIFIED_SPAWN"},
                    {"spawn_point_index": 1, "x": 1, "y": 0, "z": 0, "yaw": 0, "heavy_truck_allowed": 1, "validation_status": "VERIFIED_SPAWN"},
                ])
                store.upsert_point_conflicts([{"map_id": "m", "resource_version": "v", "point_a_id": "carla-spawn:0", "point_b_id": "carla-spawn:1", "conflict_type": "STATIC_CENTER_DISTANCE", "minimum_clearance_m": 1, "validation_status": "STATIC_INFERRED", "source": "TEST"}])
                pairs = select_static_candidates(store, "m", "v", 1)
                world = World()
                result = verify_dual_spawn_pairs(store, world, lambda point: point, "m", "v", "0325_5", pairs, run_id="dual-1")
                self.assertEqual(0, result["dual_spawn_verified"])
                self.assertEqual(1, result["dual_spawn_blocked"])
                self.assertTrue(world.actors[0].destroyed)
                status = store.connection.execute("SELECT validation_status FROM point_conflicts WHERE conflict_type='DUAL_HEAVY_TRUCK_SPAWN_CHECK'").fetchone()[0]
                self.assertEqual("DUAL_SPAWN_BLOCKED", status)
                self.assertEqual([], select_static_candidates(store, "m", "v", 1))

    def test_rpc_exception_fails_run_without_recording_false_pair_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            with MapResourceStore(Path(directory) / "map.db") as store:
                store.initialise_map("m", "m", "m", "vehicle.cat.cat", resource_version="v")
                pair = {
                    "point_a_id": "carla-spawn:0", "point_b_id": "carla-spawn:1",
                    "distance_m": 1.0, "point_a": {}, "point_b": {},
                }
                with self.assertRaisesRegex(RuntimeError, "RPC connection lost"):
                    verify_dual_spawn_pairs(
                        store, FailingWorld(), lambda point: point,
                        "m", "v", "0325_5", [pair], run_id="dual-rpc-failure",
                    )
                conflict_count = store.connection.execute(
                    "SELECT count(*) FROM point_conflicts "
                    "WHERE conflict_type='DUAL_HEAVY_TRUCK_SPAWN_CHECK'"
                ).fetchone()[0]
                run_status = store.connection.execute(
                    "SELECT status FROM calibration_runs "
                    "WHERE calibration_run_id='dual-rpc-failure'"
                ).fetchone()[0]
                self.assertEqual(0, conflict_count)
                self.assertEqual("FAILED", run_status)
