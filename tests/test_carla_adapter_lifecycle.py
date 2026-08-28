import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config
from open_pit_agent.models import Position, Task


class FakeLocalPlanner:
    def __init__(self):
        self.reset_called = False

    def reset_vehicle(self):
        self.reset_called = True


class FakeAgent:
    def __init__(self, actor=None, target_speed=0.0, done=False):
        self._local_planner = FakeLocalPlanner()
        self.destination = None
        self._done = done

    def set_destination(self, destination):
        self.destination = destination

    def done(self):
        return self._done

    def run_step(self):
        return object()


class FakeLocation:
    def __init__(self, x=1.0, y=2.0, z=3.0):
        self.x = x
        self.y = y
        self.z = z


class FakeRotation:
    def __init__(self, pitch=0.0, yaw=0.0, roll=0.0):
        self.pitch = pitch
        self.yaw = yaw
        self.roll = roll


class FakeTransform:
    def __init__(self, location=None, rotation=None):
        self.location = location or FakeLocation()
        self.rotation = rotation or FakeRotation()

    def get_right_vector(self):
        return FakeLocation(0.0, 1.0, 0.0)

    def get_forward_vector(self):
        return FakeLocation(1.0, 0.0, 0.0)


class FakeMap:
    def get_spawn_points(self):
        return [FakeTransform() for _ in range(40)]


class FakeWorld:
    def get_map(self):
        return FakeMap()

    def wait_for_tick(self, timeout_seconds):
        return object()


class FakeSpectator:
    def __init__(self):
        self.transform = None

    def set_transform(self, transform):
        self.transform = transform


class FakeActor:
    def __init__(self):
        self.location = FakeLocation()
        self.last_control = None
        self.physics_enabled = True
        self.target_velocity = None
        self.target_angular_velocity = None

    def get_location(self):
        return self.location

    def apply_control(self, control):
        self.last_control = control

    def get_transform(self):
        return FakeTransform(self.location)

    def set_transform(self, transform):
        self.location = transform.location

    def set_simulate_physics(self, enabled):
        self.physics_enabled = enabled

    def set_target_velocity(self, velocity):
        self.target_velocity = velocity

    def set_target_angular_velocity(self, velocity):
        self.target_angular_velocity = velocity


class FakeCarla:
    @staticmethod
    def VehicleControl(**kwargs):
        return kwargs

    @staticmethod
    def Location(**kwargs):
        return FakeLocation(**kwargs)

    @staticmethod
    def Rotation(**kwargs):
        return FakeRotation(**kwargs)

    @staticmethod
    def Transform(location, rotation):
        return FakeTransform(location, rotation)

    @staticmethod
    def Vector3D(**kwargs):
        return FakeLocation(**kwargs)


class CarlaAdapterLifecycleTest(unittest.TestCase):
    def test_initially_idle_vehicle_stays_at_configured_spawn(self):
        config = load_config(
            PROJECT_ROOT
            / "configs"
            / "town03_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.carla = FakeCarla()
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor

        adapter._start_next_task(vehicle_id)

        self.assertNotIn(vehicle_id, adapter._parked)
        self.assertEqual(2.0, actor.location.y)
        self.assertEqual([], adapter.drain_events())

    def test_empty_queue_parks_vehicle_off_shared_lane(self):
        config = load_config(
            PROJECT_ROOT
            / "configs"
            / "town03_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.carla = FakeCarla()
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        adapter._vehicles_with_completed_task.add(vehicle_id)

        adapter._start_next_task(vehicle_id)

        self.assertIn(vehicle_id, adapter._parked)
        self.assertEqual(14.0, actor.location.y)
        self.assertTrue(actor.last_control["hand_brake"])
        self.assertEqual(
            "vehicle_parked_off_route",
            adapter.drain_events()[0]["event_type"],
        )

    def test_last_completed_task_stops_without_lateral_teleport(self):
        config = load_config(
            PROJECT_ROOT
            / "configs"
            / "town03_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.carla = FakeCarla()
        vehicle_id = "emergency_vehicle_01"
        actor = FakeActor()
        completed_task = Task(
            task_id="last-task",
            zone_id="routine_zone_01",
            priority=30,
            required_capabilities=["inspection"],
            status="completed",
            assigned_vehicle_id=vehicle_id,
        )
        adapter._actors[vehicle_id] = actor
        adapter._task_objects[completed_task.task_id] = completed_task
        adapter._vehicles_with_completed_task.add(vehicle_id)

        adapter._start_next_task(vehicle_id)

        self.assertNotIn(vehicle_id, adapter._parked)
        self.assertEqual(2.0, actor.location.y)
        self.assertTrue(actor.last_control["hand_brake"])
        self.assertEqual(0.0, actor.target_velocity.x)
        self.assertEqual(0.0, actor.target_angular_velocity.z)
        self.assertEqual([], adapter.drain_events())

    def test_spectator_follows_highest_priority_task(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "town03.json"
        )
        adapter = CarlaAdapter(config)
        adapter.carla = FakeCarla()
        adapter._spectator = FakeSpectator()
        low_vehicle = "inspection_vehicle_01"
        high_vehicle = "inspection_vehicle_02"
        adapter._actors[low_vehicle] = FakeActor()
        adapter._actors[high_vehicle] = FakeActor()
        low_task = Task(
            task_id="low-task",
            zone_id="inspection_zone_01",
            priority=30,
            required_capabilities=["inspection"],
        )
        high_task = Task(
            task_id="high-task",
            zone_id="inspection_zone_02",
            priority=100,
            required_capabilities=["inspection"],
        )
        adapter._task_objects = {
            low_task.task_id: low_task,
            high_task.task_id: high_task,
        }
        adapter._task_ids = {
            low_vehicle: low_task.task_id,
            high_vehicle: high_task.task_id,
        }

        adapter._update_spectator_camera()

        self.assertEqual(
            high_vehicle, adapter._camera_vehicle_id
        )
        self.assertIsNotNone(
            adapter._spectator.transform
        )
        self.assertEqual(
            -8.0,
            adapter._spectator.transform.location.x,
        )
        event = adapter.drain_events()[0]
        self.assertEqual(
            "spectator_camera_target_changed",
            event["event_type"],
        )
        self.assertEqual(
            high_vehicle, event["payload"]["vehicle_id"]
        )

    def test_terminal_vehicle_applies_hand_brake(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "town03.json"
        )
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        vehicle_id = "inspection_vehicle_01"
        actor = FakeActor()
        task = Task(
            task_id="only-task",
            zone_id="inspection_zone_01",
            priority=30,
            required_capabilities=["inspection"],
            status="executing",
            assigned_vehicle_id=vehicle_id,
        )
        adapter._actors[vehicle_id] = actor
        adapter._agents[vehicle_id] = FakeAgent()
        adapter._task_ids[vehicle_id] = task.task_id
        adapter._task_objects[task.task_id] = task
        adapter._task_queues[vehicle_id] = [task.task_id]
        adapter._task_targets[task.task_id] = Position(
            actor.location.x,
            actor.location.y,
            actor.location.z,
        )
        adapter._task_started_ticks[task.task_id] = 0

        adapter.tick()

        self.assertEqual("completed", task.status)
        self.assertTrue(actor.last_control["hand_brake"])
        self.assertEqual(
            "idle", adapter._task_status[vehicle_id]
        )

    def test_release_agent_detaches_vehicle_before_disposal(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        agent = FakeAgent()
        adapter._agents["inspection_vehicle_01"] = agent

        adapter._release_agent("inspection_vehicle_01")

        self.assertTrue(agent._local_planner.reset_called)
        self.assertNotIn("inspection_vehicle_01", adapter._agents)

    def test_higher_priority_task_preempts_current_task(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()
        old_agent = FakeAgent()
        adapter._agents[vehicle_id] = old_agent
        adapter._task_ids[vehicle_id] = "normal-task"

        normal = Task(
            task_id="normal-task",
            zone_id="inspection_zone_03",
            priority=50,
            required_capabilities=["inspection"],
            status="executing",
            assigned_vehicle_id=vehicle_id,
        )
        urgent = Task(
            task_id="urgent-task",
            zone_id="inspection_zone_02",
            priority=100,
            required_capabilities=["inspection", "camera"],
            status="assigned",
            assigned_vehicle_id=vehicle_id,
        )

        adapter.dispatch([normal, urgent], config.zones)

        self.assertTrue(old_agent._local_planner.reset_called)
        self.assertEqual("assigned", normal.status)
        self.assertEqual("executing", urgent.status)
        self.assertEqual("urgent-task", adapter._task_ids[vehicle_id])

    def test_completed_task_starts_next_queued_task(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor

        first = Task(
            task_id="first-task",
            zone_id="inspection_zone_02",
            priority=100,
            required_capabilities=["inspection", "camera"],
            status="executing",
            assigned_vehicle_id=vehicle_id,
            started_tick=0,
        )
        second = Task(
            task_id="second-task",
            zone_id="inspection_zone_03",
            priority=50,
            required_capabilities=["inspection"],
            status="assigned",
            assigned_vehicle_id=vehicle_id,
        )
        adapter._task_objects = {
            first.task_id: first,
            second.task_id: second,
        }
        adapter._zones_by_id = {
            zone.zone_id: zone for zone in config.zones
        }
        adapter._task_queues[vehicle_id] = [first.task_id, second.task_id]
        adapter._task_ids[vehicle_id] = first.task_id
        adapter._task_targets[first.task_id] = Position(1.0, 2.0, 3.0)
        adapter._task_started_ticks[first.task_id] = 0
        adapter._agents[vehicle_id] = FakeAgent(done=True)

        adapter.tick()

        self.assertEqual("completed", first.status)
        self.assertIsNotNone(first.completed_at)
        self.assertEqual("executing", second.status)
        self.assertEqual(second.task_id, adapter._task_ids[vehicle_id])
        event_types = {
            event["event_type"] for event in adapter.drain_events()
        }
        self.assertIn("task_completed", event_types)
        self.assertIn("task_started", event_types)

    def test_safe_route_waypoint_is_a_navigation_leg_not_completion(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        task = Task(
            task_id="risk-review-test",
            zone_id="risk_zone_02",
            priority=260,
            required_capabilities=["inspection", "camera", "lidar"],
            task_type="risk_review",
            status="assigned",
            assigned_vehicle_id=vehicle_id,
        )

        adapter.dispatch([task], config.zones)
        route = adapter.configure_safe_route(
            route_plan_id="safe-route-test",
            waypoint_spawn_point_index=1,
            task_types=["risk_review"],
            blocked_road_segment_id="blocked-road-test",
        )
        self.assertTrue(
            adapter._route_remaining_targets[task.task_id]
        )
        self.assertEqual("safe-route-test", route["route_plan_id"])
        self.assertEqual(
            [task.task_id], route["replanned_task_ids"]
        )

        adapter.tick()

        self.assertEqual("executing", task.status)
        self.assertEqual(task.task_id, adapter._task_ids[vehicle_id])
        self.assertEqual([], adapter._route_remaining_targets[task.task_id])
        event_types = [
            event["event_type"] for event in adapter.drain_events()
        ]
        self.assertIn("safe_route_activated", event_types)
        self.assertIn("safe_route_waypoint_reached", event_types)
        self.assertNotIn("task_completed", event_types)

        adapter.tick()
        self.assertEqual("completed", task.status)
        cleared = adapter.clear_safe_route("feedback_safe")
        self.assertEqual("safe-route-test", cleared["route_plan_id"])
        self.assertIsNone(adapter._safe_route_plan)
        self.assertIn(
            "safe_route_deactivated",
            [event["event_type"] for event in adapter.drain_events()],
        )

    def test_planner_done_outside_tolerance_does_not_complete_task(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()
        task = Task(
            task_id="far-task",
            zone_id="inspection_zone_02",
            priority=100,
            required_capabilities=["inspection", "camera"],
            status="executing",
            assigned_vehicle_id=vehicle_id,
            started_tick=0,
        )
        adapter._task_objects[task.task_id] = task
        adapter._task_queues[vehicle_id] = [task.task_id]
        adapter._task_ids[vehicle_id] = task.task_id
        adapter._task_targets[task.task_id] = Position(50.0, 2.0, 3.0)
        adapter._task_started_ticks[task.task_id] = 0
        adapter._agents[vehicle_id] = FakeAgent(done=True)

        adapter.tick()

        self.assertEqual("executing", task.status)
        self.assertEqual(task.task_id, adapter._task_ids[vehicle_id])
        self.assertNotIn(
            "task_completed",
            {
                event["event_type"]
                for event in adapter.drain_events()
            },
        )

    def test_fault_can_pull_vehicle_over_before_disabling_it(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "town03_fault_recovery.json"
        )
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        vehicle_id = config.demo.failure_vehicle_id
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        adapter._agents[vehicle_id] = FakeAgent()
        adapter._task_ids[vehicle_id] = "interrupted-task"

        adapter.inject_fault(vehicle_id)

        expected_y = 2.0 + config.demo.fault_pull_over_offset_m
        self.assertAlmostEqual(expected_y, actor.location.y)
        self.assertTrue(actor.last_control["hand_brake"])
        self.assertFalse(actor.physics_enabled)
        event = adapter.drain_events()[0]
        self.assertEqual("vehicle_fault_applied", event["event_type"])
        self.assertEqual(
            "safe_pull_over_then_disable",
            event["payload"]["fault_mode"],
        )
        self.assertEqual(
            config.demo.fault_pull_over_offset_m,
            event["payload"]["pull_over_offset_m"],
        )
        self.assertEqual(
            expected_y,
            event["payload"]["safe_stop_target_position"]["y"],
        )
        self.assertTrue(event["payload"]["physics_disabled"])


if __name__ == "__main__":
    unittest.main()
