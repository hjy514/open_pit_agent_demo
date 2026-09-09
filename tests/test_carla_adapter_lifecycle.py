import sys
import os
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import load_config
from open_pit_agent.models import Position, Task


class FakeLocalPlanner:
    def __init__(self):
        self.reset_called = False
        self.speed = None

    def reset_vehicle(self):
        self.reset_called = True

    def set_speed(self, speed):
        self.speed = speed


class FakeAgent:
    def __init__(self, actor=None, target_speed=0.0, done=False):
        self._local_planner = FakeLocalPlanner()
        self.destination = None
        self._done = done
        self.target_speed = target_speed

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
    def __init__(self, actor_id=1):
        self.id = actor_id
        self.destroyed = False
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

    def destroy(self):
        self.destroyed = True
        return True


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
    def test_pause_and_resume_ignore_vehicle_already_retired_from_episode(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        vehicle_id = "inspection_vehicle_02"
        adapter._task_status[vehicle_id] = "retired_after_execution_failure"

        paused = adapter.pause_vehicle(vehicle_id)
        resumed = adapter.resume_vehicle(vehicle_id)

        self.assertEqual("ALREADY_RETIRED", paused["status"])
        self.assertEqual("ALREADY_RETIRED", resumed["status"])

    def test_resume_rebuilds_active_navigation_from_current_state(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()
        task = Task(
            task_id="pause-resume-task",
            zone_id="inspection_zone_02",
            priority=50,
            required_capabilities=["inspection", "camera"],
            status="assigned",
            assigned_vehicle_id=vehicle_id,
        )

        adapter.dispatch([task], config.zones)
        original_agent = adapter._agents[vehicle_id]
        original_target = adapter._task_targets[task.task_id]
        adapter.pause_vehicle(vehicle_id)
        adapter._tick_index = 25
        result = adapter.resume_vehicle(vehicle_id)

        self.assertEqual("executing", result["status"])
        self.assertIsNot(original_agent, adapter._agents[vehicle_id])
        self.assertTrue(original_agent._local_planner.reset_called)
        self.assertEqual(
            [original_target.x, original_target.y, original_target.z],
            adapter._agents[vehicle_id].destination,
        )
        self.assertIn(
            "vehicle_navigation_refreshed_after_resume",
            [item["event_type"] for item in adapter.drain_events()],
        )

    def test_completed_owned_vehicle_retires_while_other_tasks_are_active(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor(actor_id=501)
        adapter._actors[vehicle_id] = actor
        adapter.spawned_actor_ids = [501]
        completed = Task(
            task_id="completed-task", zone_id="inspection_zone_02",
            priority=50, required_capabilities=["inspection"],
            status="executing", assigned_vehicle_id=vehicle_id,
        )
        other = Task(
            task_id="other-active-task", zone_id="inspection_zone_01",
            priority=50, required_capabilities=["inspection"],
            status="executing", assigned_vehicle_id="other-truck",
        )
        adapter._task_objects = {
            completed.task_id: completed, other.task_id: other,
        }
        adapter._task_ids[vehicle_id] = completed.task_id
        adapter._task_queues[vehicle_id] = [completed.task_id]

        adapter._complete_task(vehicle_id, completed.task_id)

        self.assertEqual("completed", completed.status)
        self.assertTrue(actor.destroyed)
        self.assertNotIn(vehicle_id, adapter._actors)
        retirement = next(
            item for item in adapter.drain_events()
            if item["event_type"] == "vehicle_retired_from_episode"
        )
        self.assertEqual(
            "RETIRED_FROM_EPISODE", retirement["payload"]["status"]
        )

    def test_task_mission_requires_deadhead_before_service_completion(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        task = Task(
            task_id="deadhead-service-task",
            zone_id="inspection_zone_02",
            priority=50,
            required_capabilities=["inspection", "camera"],
            status="assigned",
            assigned_vehicle_id=vehicle_id,
        )
        adapter._actors[vehicle_id] = FakeActor()
        configured = adapter.configure_task_missions([{
            "task_id": task.task_id,
            "vehicle_id": vehicle_id,
            "service_origin_spawn_point_index": 1,
            "service_target_spawn_point_index": 2,
            "service_origin_point_id": "carla-spawn:1",
            "service_target_point_id": "carla-spawn:2",
            "requires_deadhead": True,
        }])

        adapter.dispatch([task], config.zones)
        self.assertEqual("CONFIGURED", configured["status"])
        self.assertEqual("deadhead_to_service_origin",
                         adapter._task_status[vehicle_id])
        self.assertEqual("to_service_origin",
                         adapter._mission_plans[task.task_id]["phase"])

        adapter.tick()
        self.assertEqual("executing", task.status)
        self.assertEqual("service_execution",
                         adapter._mission_plans[task.task_id]["phase"])
        adapter.tick()
        self.assertEqual("completed", task.status)
        transitions = [
            item["payload"]["to_status"] for item in adapter.drain_events()
            if item["event_type"] == "task_mission_stage_changed"
        ]
        self.assertEqual(
            ["to_service_origin", "at_service_origin",
             "service_execution", "terminal"],
            transitions,
        )

    def test_common_production_cycle_loads_hauls_dumps_and_returns(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        task = Task(
            task_id="production-task", zone_id="inspection_zone_02",
            priority=100, required_capabilities=["inspection", "camera"],
            status="assigned", assigned_vehicle_id=vehicle_id,
        )

        configured = adapter.configure_production_cycles([{
            "task_id": task.task_id,
            "origin_spawn_point_index": 0,
            "loading_ticks": 1,
            "dumping_ticks": 1,
            "return_route_evidence": "TEST_ROUTE_FACT",
        }])
        adapter.dispatch([task], config.zones)

        self.assertEqual("CONFIGURED", configured["status"])
        self.assertEqual("loading", adapter._task_status[vehicle_id])
        adapter.tick()
        adapter.tick()
        self.assertEqual("dumping", adapter._task_status[vehicle_id])
        adapter.tick()
        self.assertEqual("completed", task.status)
        event_types = [item["event_type"] for item in adapter.drain_events()]
        self.assertGreaterEqual(
            event_types.count("task_production_stage_changed"), 4
        )
        self.assertIn("task_completed", event_types)

    def test_directed_haul_task_completes_after_dumping_without_return(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        task = Task(
            task_id="directed-haul-task", zone_id="inspection_zone_02",
            priority=100, required_capabilities=["inspection", "camera"],
            status="assigned", assigned_vehicle_id=vehicle_id,
            task_type="haul_transport",
        )
        adapter.configure_production_cycles([{
            "task_id": task.task_id,
            "origin_spawn_point_index": 0,
            "loading_ticks": 1,
            "dumping_ticks": 1,
            "completion_after_dumping": True,
            "task_completion_semantics": "destination_service_completed",
        }])
        adapter.dispatch([task], config.zones)

        adapter.tick()
        adapter.tick()
        self.assertEqual("dumping", adapter._task_status[vehicle_id])
        adapter.tick()

        self.assertEqual("completed", task.status)
        self.assertEqual("destination_service_completed", task.status_reason)
        self.assertNotIn(vehicle_id, adapter._agents)
        stage_events = [
            item for item in adapter.drain_events()
            if item["event_type"] == "task_production_stage_changed"
        ]
        self.assertEqual("terminal", stage_events[-1]["payload"]["to_status"])

    def test_destroy_spawned_vehicles_only_removes_owned_actors(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        owned = FakeActor(actor_id=101)
        discovered = FakeActor(actor_id=202)
        adapter._actors = {"owned": owned, "discovered": discovered}
        adapter.spawned_actor_ids = [101]

        self.assertEqual(1, adapter.destroy_spawned_vehicles())
        self.assertTrue(owned.destroyed)
        self.assertFalse(discovered.destroyed)
        self.assertNotIn("owned", adapter._actors)
        self.assertIn("discovered", adapter._actors)

    def test_local_environment_root_overrides_stale_configured_root(self):
        config = load_config(PROJECT_ROOT / "configs" / "mine_competition_demo.json")
        adapter = CarlaAdapter(config)
        with patch.dict(os.environ, {"OPENPIT_CARLA_ROOT": "/tmp/local-carla"}):
            with patch("open_pit_agent.adapters.carla_adapter.glob.glob", return_value=[]):
                with self.assertRaises(CarlaAdapterError) as raised:
                    adapter._import_carla()
        self.assertIn("/tmp/local-carla/PythonAPI/carla/dist", str(raised.exception))

    def test_hazard_route_progress_is_frozen_ahead_of_affected_truck(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        vehicle_id = "inspection_vehicle_01"
        actor = FakeActor()
        actor.location = FakeLocation(1.0, 0.0, 0.0)
        task = Task(
            task_id="original-haul-task",
            zone_id="routine_zone_01",
            priority=100,
            required_capabilities=["inspection"],
            status="executing",
            assigned_vehicle_id=vehicle_id,
            original_route=[
                {"x": float(x), "y": 0.0, "z": 0.0}
                for x in (0, 20, 40, 60, 80)
            ],
        )
        adapter._actors[vehicle_id] = actor
        adapter._task_objects[task.task_id] = task
        adapter._task_ids[vehicle_id] = task.task_id

        result = adapter.freeze_hazard_task_route(
            vehicle_id, clearance_m=50.0
        )

        self.assertEqual(60.0, result["safe_merge_point"]["x"])
        self.assertEqual(60.0, task.safe_merge_point["x"])
        self.assertEqual([60.0, 80.0], [
            point["x"] for point in task.remaining_route
        ])
        self.assertEqual(
            "hazard_task_route_frozen",
            adapter.drain_events()[0]["event_type"],
        )

    def test_takeover_speed_limit_reaches_carla_local_planner(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        task_id = "takeover-task"
        adapter._actors[vehicle_id] = FakeActor()
        adapter._task_speed_limits[task_id] = 15.0

        adapter._start_navigation_leg(
            vehicle_id, task_id, Position(20.0, 0.0, 0.0)
        )

        agent = adapter._agents[vehicle_id]
        self.assertEqual(15.0, agent.target_speed)
        self.assertEqual(15.0, agent._local_planner.speed)

    def test_speed_limit_during_production_hold_does_not_start_second_agent(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        task_id = "production-hold-task"
        adapter._actors[vehicle_id] = FakeActor()
        task = Task(
            task_id=task_id, zone_id="inspection_zone_02",
            priority=100, required_capabilities=["inspection", "camera"],
            status="assigned", assigned_vehicle_id=vehicle_id,
        )
        adapter.configure_production_cycles([{
            "task_id": task_id,
            "origin_spawn_point_index": 0,
            "loading_ticks": 10,
            "dumping_ticks": 10,
        }])
        adapter.dispatch([task], config.zones)

        response = adapter.set_task_speed_limit(task_id, 15.0)

        self.assertEqual(15.0, response["speed_limit_kmh"])
        self.assertEqual("loading", adapter._task_status[vehicle_id])
        self.assertNotIn(vehicle_id, adapter._agents)
        self.assertEqual(15.0, adapter._task_speed_limits[task_id])

        tolerance = adapter.set_task_arrival_tolerance(task_id, 12.0)
        self.assertEqual(12.0, tolerance["arrival_tolerance_m"])
        self.assertEqual(12.0, adapter._task_arrival_tolerances[task_id])

    def test_starting_new_navigation_leg_safely_releases_existing_agent(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()
        existing_agent = FakeAgent()
        existing_planner = existing_agent._local_planner
        adapter._agents[vehicle_id] = existing_agent

        adapter._start_navigation_leg(
            vehicle_id, "replacement-task", Position(20.0, 0.0, 0.0)
        )

        self.assertTrue(existing_planner.reset_called)
        self.assertIsNot(existing_agent, adapter._agents[vehicle_id])

    def test_selected_road_graph_route_becomes_successive_navigation_legs(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        task_id = "road-closure-task"
        adapter._actors[vehicle_id] = FakeActor()
        task = Task(
            task_id=task_id, zone_id="inspection_zone_02",
            priority=100, required_capabilities=["inspection", "camera"],
            status="executing", assigned_vehicle_id=vehicle_id,
        )
        adapter._task_objects[task_id] = task
        adapter._zones_by_id = {
            zone.zone_id: zone for zone in config.zones
        }
        adapter._task_ids[vehicle_id] = task_id
        adapter._agents[vehicle_id] = FakeAgent()

        response = adapter.set_task_route(
            task_id, vehicle_id,
            [{"x": 10.0, "y": 0.0, "z": 0.0},
             {"x": 20.0, "y": 0.0, "z": 0.0}],
            blocked_edge_id="edge-closed",
            route_edge_ids=["edge-safe-1", "edge-safe-2"],
        )

        self.assertEqual("executing", response["status"])
        self.assertEqual("edge-closed", response["blocked_edge_id"])
        self.assertEqual(3, response["waypoint_count"])
        self.assertEqual(2, len(adapter._route_remaining_targets[task_id]))
        self.assertEqual([10.0, 0.0, 0.0],
                         adapter._agents[vehicle_id].destination)
        self.assertEqual("road_graph_route_applied", task.status_reason)

    def test_road_graph_intermediate_checkpoint_uses_truck_clearance_tolerance(self):
        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        vehicle_id = "inspection_vehicle_02"
        task_id = "road-graph-checkpoint-task"
        actor = FakeActor()
        adapter._actors[vehicle_id] = actor
        task = Task(
            task_id=task_id, zone_id="inspection_zone_02",
            priority=100, required_capabilities=["inspection", "camera"],
            status="executing", assigned_vehicle_id=vehicle_id,
        )
        adapter._task_objects[task_id] = task
        adapter._task_ids[vehicle_id] = task_id
        adapter._task_targets[task_id] = Position(16.0, 2.0, 3.0)
        adapter._route_remaining_targets[task_id] = [
            Position(30.0, 2.0, 3.0)
        ]
        adapter._task_arrival_tolerances[task_id] = 12.0
        adapter._agents[vehicle_id] = FakeAgent()

        adapter.tick()

        self.assertEqual(
            Position(30.0, 2.0, 3.0), adapter._task_targets[task_id]
        )
        self.assertEqual([], adapter._route_remaining_targets[task_id])
        self.assertEqual(
            "safe_route_waypoint_reached", task.status_reason
        )

    def test_route_initialization_failure_detaches_local_planner_before_reraising(self):
        class FailingAgent(FakeAgent):
            last_instance = None

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                FailingAgent.last_instance = self

            def set_destination(self, destination):
                raise RuntimeError("synthetic route initialization failure")

        config = load_config(PROJECT_ROOT / "configs" / "town03.json")
        adapter = CarlaAdapter(config)
        adapter._basic_agent_class = FailingAgent
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()

        with self.assertRaises(CarlaAdapterError) as raised:
            adapter._start_navigation_leg(
                vehicle_id, "failing-task", Position(20.0, 0.0, 0.0)
            )
        self.assertIn("synthetic route initialization failure", str(raised.exception))
        self.assertNotIn(vehicle_id, adapter._agents)
        self.assertTrue(FailingAgent.last_instance._local_planner.reset_called)

    def test_mine_demo_uses_selected_vehicle_direct_takeover_route(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        vehicle_id = "inspection_vehicle_02"
        adapter._actors[vehicle_id] = FakeActor()
        task = Task(
            task_id="configured-takeover-task",
            zone_id="routine_zone_01",
            priority=30,
            required_capabilities=["inspection", "routine_patrol"],
            handover_reason="released_after_slope_hazard_safe_hold",
            original_vehicle_id="inspection_vehicle_01",
        )

        route = adapter._build_takeover_route(task, vehicle_id)

        self.assertEqual(1, len(route))
        self.assertEqual(1, len(task.remaining_route))
        event = adapter.drain_events()[0]
        self.assertEqual(
            "hazard_task_takeover_route_built", event["event_type"]
        )
        self.assertEqual(
            "nearest_capable_vehicle_direct_to_original_destination",
            event["payload"]["strategy"],
        )
        self.assertFalse(event["payload"]["blocked_segment_is_skipped"])

        emergency_vehicle_id = "emergency_vehicle_01"
        adapter._actors[emergency_vehicle_id] = FakeActor()
        emergency_route = adapter._build_takeover_route(
            task, emergency_vehicle_id
        )
        self.assertEqual(1, len(emergency_route))

    def test_manual_takeover_preserves_selected_vehicle_original_task(self):
        config = load_config(
            PROJECT_ROOT / "configs" / "mine_competition_demo.json"
        )
        adapter = CarlaAdapter(config)
        adapter.world = FakeWorld()
        adapter.carla = FakeCarla()
        adapter._basic_agent_class = FakeAgent
        source_vehicle = "inspection_vehicle_01"
        selected_vehicle = "inspection_vehicle_02"
        adapter._actors[source_vehicle] = FakeActor()
        adapter._actors[selected_vehicle] = FakeActor()
        own_task = Task(
            task_id="selected-original-task",
            zone_id="secondary_patrol_zone_02",
            priority=50,
            required_capabilities=["inspection", "lidar"],
            status="executing",
            assigned_vehicle_id=selected_vehicle,
        )
        takeover_task = Task(
            task_id="affected-takeover-task",
            zone_id="routine_zone_01",
            priority=30,
            required_capabilities=["inspection", "routine_patrol"],
            status="pending",
            original_vehicle_id=source_vehicle,
            handover_reason="released_after_slope_hazard_safe_hold",
        )
        adapter._task_objects = {
            own_task.task_id: own_task,
            takeover_task.task_id: takeover_task,
        }
        adapter._zones_by_id = {
            zone.zone_id: zone for zone in config.zones
        }
        adapter._task_ids = {
            source_vehicle: takeover_task.task_id,
            selected_vehicle: own_task.task_id,
        }
        adapter._task_queues = {
            source_vehicle: [takeover_task.task_id],
            selected_vehicle: [own_task.task_id],
        }
        adapter._agents = {
            source_vehicle: FakeAgent(),
            selected_vehicle: FakeAgent(),
        }
        adapter._emergency_stopped.add(source_vehicle)

        adapter.reassign_task(
            takeover_task.task_id, selected_vehicle,
            assignment_source="scenario_event",
        )

        self.assertNotIn(source_vehicle, adapter._task_ids)
        self.assertEqual(
            "emergency_stop", adapter._task_status[source_vehicle]
        )
        self.assertEqual("assigned", own_task.status)
        self.assertEqual(
            [takeover_task.task_id, own_task.task_id],
            adapter._task_queues[selected_vehicle],
        )
        self.assertEqual(
            takeover_task.task_id,
            adapter._task_ids[selected_vehicle],
        )
        events = adapter.drain_events()
        reassignment = next(
            item for item in events
            if item["event_type"] == "task_reassigned_by_scenario"
        )
        self.assertEqual("scenario_event", reassignment["payload"]["assignment_source"])

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
            -19.0,
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
            waypoint_spawn_point_indices=[1, 2, 3],
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
        self.assertEqual(
            2, len(adapter._route_remaining_targets[task.task_id])
        )
        event_types = [
            event["event_type"] for event in adapter.drain_events()
        ]
        self.assertIn("safe_route_activated", event_types)
        self.assertIn("safe_route_waypoint_reached", event_types)
        self.assertNotIn("task_completed", event_types)

        adapter.tick()
        self.assertEqual("executing", task.status)
        adapter.tick()
        self.assertEqual("executing", task.status)
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
