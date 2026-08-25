"""CARLA 0.9.10 adapter kept separate from scheduling business logic."""

import glob
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .base import EquipmentAdapter
from ..config import ScenarioConfig, VehicleConfig, ZoneConfig
from ..models import Position, Task, VehicleState, utc_now


class CarlaAdapterError(RuntimeError):
    """Raised when CARLA cannot satisfy the configured operation."""


class CarlaAdapter(EquipmentAdapter):
    def __init__(self, config: ScenarioConfig, load_map: bool = False) -> None:
        self.config = config
        self.load_map = load_map
        self.client = None
        self.world = None
        self.carla = None
        self._basic_agent_class = None
        self._actors: Dict[str, object] = {}
        self._agents: Dict[str, object] = {}
        self._task_ids: Dict[str, str] = {}
        self._task_status: Dict[str, str] = {}
        self._task_objects: Dict[str, Task] = {}
        self._task_queues: Dict[str, List[str]] = {}
        self._zones_by_id: Dict[str, ZoneConfig] = {}
        self._task_targets: Dict[str, Position] = {}
        self._task_started_ticks: Dict[str, int] = {}
        self._events: List[Dict[str, object]] = []
        self._tick_index = 0
        self._faulted = set()
        self._paused = set()
        self._emergency_stopped = set()
        self._pause_started_ticks: Dict[str, int] = {}
        self._parked = set()
        self._vehicles_with_completed_task = set()
        self._spectator = None
        self._camera_vehicle_id: Optional[str] = None
        self._trajectory_points: Dict[str, List[Dict[str, float]]] = {}
        self._road_segments_cache: Optional[List[Dict[str, object]]] = None
        self._map_bounds_cache: Optional[Dict[str, float]] = None
        self.spawned_actor_ids: List[int] = []

    def connect(self) -> None:
        self._import_carla()
        try:
            self.client = self.carla.Client(
                self.config.carla.host, self.config.carla.port
            )
            self.client.set_timeout(self.config.carla.timeout_seconds)
            if self.load_map:
                self.world = self.client.load_world(self.config.carla.map_name)
            else:
                self.world = self.client.get_world()
        except RuntimeError as exc:
            raise CarlaAdapterError(
                "Unable to connect/load CARLA at {}:{}: {}".format(
                    self.config.carla.host, self.config.carla.port, exc
                )
            ) from exc

        current_map = self.world.get_map().name.split("/")[-1]
        if current_map != self.config.carla.map_name:
            raise CarlaAdapterError(
                "Current CARLA map is {}, expected {}. Re-run with --load-map "
                "or load the map manually.".format(
                    current_map, self.config.carla.map_name
                )
            )
        get_spectator = getattr(
            self.world, "get_spectator", None
        )
        if callable(get_spectator):
            self._spectator = get_spectator()
        self._discover_configured_vehicles()

    def ensure_vehicles(self, spawn_missing: bool) -> None:
        self._require_connected()
        if not spawn_missing:
            return
        spawn_points = self.world.get_map().get_spawn_points()
        if not spawn_points:
            raise CarlaAdapterError("Current map has no vehicle spawn points")

        spawned_count = 0
        for definition in self.config.vehicles:
            if definition.vehicle_id in self._actors:
                continue
            blueprint = self._get_blueprint(definition)
            blueprint.set_attribute("role_name", definition.role_name)
            preferred_index = definition.spawn_point_index % len(spawn_points)
            candidate_indices = [preferred_index] + [
                index
                for index in range(len(spawn_points))
                if index != preferred_index
            ]
            actor = None
            selected_index = None
            for spawn_index in candidate_indices:
                actor = self.world.try_spawn_actor(
                    blueprint, spawn_points[spawn_index]
                )
                if actor is not None:
                    selected_index = spawn_index
                    break
            if actor is None:
                raise CarlaAdapterError(
                    "Failed to spawn {}: all {} spawn points are occupied or unsafe".format(
                        definition.vehicle_id, len(spawn_points)
                    )
                )
            self._actors[definition.vehicle_id] = actor
            self.spawned_actor_ids.append(actor.id)
            spawned_count += 1
            print(
                "已生成 {}：actor_id={}，出生点={}（首选={}）".format(
                    definition.vehicle_id,
                    actor.id,
                    selected_index,
                    preferred_index,
                )
            )
        if spawned_count:
            try:
                self.world.wait_for_tick(self.config.carla.timeout_seconds)
            except RuntimeError as exc:
                raise CarlaAdapterError(
                    "Vehicles spawned, but CARLA did not produce a stabilization "
                    "tick: {}".format(exc)
                ) from exc

    def list_states(self) -> Sequence[VehicleState]:
        self._require_connected()
        definitions = {
            item.vehicle_id: item for item in self.config.vehicles
        }
        states = []
        for vehicle_id in sorted(self._actors):
            actor = self._actors[vehicle_id]
            definition = definitions[vehicle_id]
            transform = actor.get_transform()
            location = transform.location
            yaw_deg = float(transform.rotation.yaw)
            velocity = actor.get_velocity()
            speed = math.sqrt(
                velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2
            )
            current_task_id = self._task_ids.get(vehicle_id)
            target_position = (
                self._task_targets.get(current_task_id)
                if current_task_id
                else None
            )

            self._append_trajectory_point(vehicle_id, location)
            route_points = self._extract_route_points(vehicle_id)
            trajectory_points = list(
                self._trajectory_points.get(vehicle_id, [])
            )

            faulted = vehicle_id in self._faulted
            if faulted:
                visible_status = "fault"
            elif vehicle_id in self._emergency_stopped:
                visible_status = "emergency_stop"
            elif vehicle_id in self._paused:
                visible_status = "paused"
            else:
                visible_status = self._task_status.get(
                    vehicle_id, "idle"
                )
            states.append(
                VehicleState(
                    vehicle_id=vehicle_id,
                    display_name=definition.display_name,
                    equipment_type=definition.equipment_type,
                    role_name=definition.role_name,
                    blueprint=actor.type_id,
                    capabilities=list(definition.capabilities),
                    position=Position(location.x, location.y, location.z),
                    yaw_deg=yaw_deg,
                    target_position=target_position,
                    route_points=route_points,
                    trajectory_points=trajectory_points,
                    speed_mps=speed,
                    health="fault" if faulted else "healthy",
                    available=not faulted,
                    actor_id=actor.id,
                    current_task_id=current_task_id,
                    task_status=visible_status,
                    timestamp=utc_now(),
                )
            )
        return states

    def get_map_environment(self) -> Dict[str, object]:
        """
        提取当前CARLA地图的静态道路中心线和地图边界。

        该数据只需要在一次运行开始时推送给API，后续车辆动态同步
        不必重复传输道路拓扑。
        """

        self._require_connected()
        if self._road_segments_cache is None:
            self._road_segments_cache = self._build_road_segments()
            self._map_bounds_cache = self._bounds_from_segments(
                self._road_segments_cache
            )

        return {
            "map_name": self.world.get_map().name.split("/")[-1],
            "road_segments": list(self._road_segments_cache),
            "map_bounds": dict(
                self._map_bounds_cache
                or {
                    "min_x": -50.0,
                    "max_x": 50.0,
                    "min_y": -50.0,
                    "max_y": 50.0,
                }
            ),
            "road_sampling_m": 4.0,
        }

    def _build_road_segments(self) -> List[Dict[str, object]]:
        carla_map = self.world.get_map()
        sample_distance = 4.0
        segments: List[Dict[str, object]] = []
        seen = set()

        try:
            waypoints = list(carla_map.generate_waypoints(sample_distance))
        except Exception:
            waypoints = []

        for waypoint in waypoints:
            try:
                next_waypoints = list(waypoint.next(sample_distance))
            except Exception:
                next_waypoints = []

            for next_waypoint in next_waypoints:
                start = waypoint.transform.location
                end = next_waypoint.transform.location

                if math.hypot(end.x - start.x, end.y - start.y) > 12.0:
                    continue

                key = (
                    round(start.x, 1),
                    round(start.y, 1),
                    round(end.x, 1),
                    round(end.y, 1),
                    int(getattr(waypoint, "lane_id", 0)),
                )
                reverse_key = (
                    key[2],
                    key[3],
                    key[0],
                    key[1],
                    key[4],
                )
                if key in seen or reverse_key in seen:
                    continue
                seen.add(key)

                segments.append(
                    {
                        "start": {
                            "x": float(start.x),
                            "y": float(start.y),
                            "z": float(start.z),
                        },
                        "end": {
                            "x": float(end.x),
                            "y": float(end.y),
                            "z": float(end.z),
                        },
                        "road_id": int(getattr(waypoint, "road_id", 0)),
                        "lane_id": int(getattr(waypoint, "lane_id", 0)),
                        "is_junction": bool(
                            getattr(waypoint, "is_junction", False)
                        ),
                    }
                )

                if len(segments) >= 7000:
                    return segments

        if segments:
            return segments

        # generate_waypoints异常时使用拓扑端点作为降级道路显示。
        try:
            topology = list(carla_map.get_topology())
        except Exception:
            topology = []

        for start_wp, end_wp in topology:
            start = start_wp.transform.location
            end = end_wp.transform.location
            segments.append(
                {
                    "start": {
                        "x": float(start.x),
                        "y": float(start.y),
                        "z": float(start.z),
                    },
                    "end": {
                        "x": float(end.x),
                        "y": float(end.y),
                        "z": float(end.z),
                    },
                    "road_id": int(getattr(start_wp, "road_id", 0)),
                    "lane_id": int(getattr(start_wp, "lane_id", 0)),
                    "is_junction": bool(
                        getattr(start_wp, "is_junction", False)
                    ),
                }
            )
        return segments

    @staticmethod
    def _bounds_from_segments(
        segments: Sequence[Dict[str, object]]
    ) -> Dict[str, float]:
        points = []
        for segment in segments:
            for key in ("start", "end"):
                point = segment.get(key)
                if isinstance(point, dict):
                    points.append(point)

        if not points:
            return {
                "min_x": -50.0,
                "max_x": 50.0,
                "min_y": -50.0,
                "max_y": 50.0,
            }

        xs = [float(point.get("x", 0.0)) for point in points]
        ys = [float(point.get("y", 0.0)) for point in points]
        padding = 8.0
        return {
            "min_x": min(xs) - padding,
            "max_x": max(xs) + padding,
            "min_y": min(ys) - padding,
            "max_y": max(ys) + padding,
        }

    def _append_trajectory_point(self, vehicle_id: str, location) -> None:
        history = self._trajectory_points.setdefault(vehicle_id, [])
        point = {
            "x": float(location.x),
            "y": float(location.y),
            "z": float(location.z),
        }

        if history:
            previous = history[-1]
            distance = math.sqrt(
                (point["x"] - previous["x"]) ** 2
                + (point["y"] - previous["y"]) ** 2
                + (point["z"] - previous["z"]) ** 2
            )
            if distance < 0.8:
                return

        history.append(point)
        if len(history) > 350:
            del history[:-350]

    def _extract_route_points(
        self, vehicle_id: str
    ) -> List[Dict[str, float]]:
        agent = self._agents.get(vehicle_id)
        if agent is None:
            return []

        local_planner = None
        getter = getattr(agent, "get_local_planner", None)
        if callable(getter):
            try:
                local_planner = getter()
            except Exception:
                local_planner = None
        if local_planner is None:
            local_planner = getattr(agent, "_local_planner", None)
        if local_planner is None:
            return []

        get_plan = getattr(local_planner, "get_plan", None)
        if not callable(get_plan):
            return []

        try:
            plan = list(get_plan())
        except Exception:
            return []

        points: List[Dict[str, float]] = []
        for index, plan_item in enumerate(plan[:600]):
            if index % 2 != 0 and index != len(plan) - 1:
                continue

            waypoint = (
                plan_item[0]
                if isinstance(plan_item, (tuple, list))
                and plan_item
                else plan_item
            )
            transform = getattr(waypoint, "transform", None)
            location = getattr(transform, "location", None)
            if location is None:
                continue

            point = {
                "x": float(location.x),
                "y": float(location.y),
                "z": float(location.z),
            }
            if points:
                previous = points[-1]
                if math.hypot(
                    point["x"] - previous["x"],
                    point["y"] - previous["y"],
                ) < 1.0:
                    continue
            points.append(point)

        target_task_id = self._task_ids.get(vehicle_id)
        target = (
            self._task_targets.get(target_task_id)
            if target_task_id
            else None
        )
        if target is not None:
            target_point = {
                "x": float(target.x),
                "y": float(target.y),
                "z": float(target.z),
            }
            if not points or math.hypot(
                target_point["x"] - points[-1]["x"],
                target_point["y"] - points[-1]["y"],
            ) > 1.0:
                points.append(target_point)

        return points

    def resolve_zones(
        self, zones: Sequence[ZoneConfig]
    ) -> List[ZoneConfig]:
        """Resolve configured spawn-point targets to current-map coordinates."""

        self._require_connected()
        spawn_points = self.world.get_map().get_spawn_points()
        if not spawn_points:
            raise CarlaAdapterError("Current map has no vehicle spawn points")
        resolved = []
        for zone in zones:
            index = zone.target_spawn_point_index % len(spawn_points)
            location = spawn_points[index].location
            resolved.append(
                replace(
                    zone,
                    mock_position=Position(
                        location.x, location.y, location.z
                    ),
                )
            )
        return resolved

    def dispatch(self, tasks: Sequence[Task], zones: Sequence[ZoneConfig]) -> None:
        self._require_connected()
        self._zones_by_id = {zone.zone_id: zone for zone in zones}
        self._task_objects = {task.task_id: task for task in tasks}
        tasks_by_vehicle: Dict[str, List[Task]] = {}
        for task in tasks:
            if task.assigned_vehicle_id and task.status not in {
                "completed",
                "timed_out",
                "cancelled",
            }:
                tasks_by_vehicle.setdefault(task.assigned_vehicle_id, []).append(task)

        for vehicle_id in self._actors:
            vehicle_tasks = tasks_by_vehicle.get(vehicle_id, [])
            self._task_queues[vehicle_id] = [
                task.task_id
                for task in sorted(
                    vehicle_tasks, key=lambda item: (-item.priority, item.task_id)
                )
            ]
            if vehicle_id in self._faulted:
                continue
            current_task_id = self._task_ids.get(vehicle_id)
            if current_task_id and current_task_id in self._task_queues[vehicle_id]:
                next_task_id = self._task_queues[vehicle_id][0]
                current_task = self._task_objects[current_task_id]
                next_task = self._task_objects[next_task_id]
                if (
                    next_task_id == current_task_id
                    or next_task.priority <= current_task.priority
                ):
                    continue
                current_task.status = "assigned"
                current_task.updated_at = utc_now()
                current_task.started_at = None
                current_task.started_tick = None
                current_task.status_reason = "preempted_by_higher_priority"
                self._release_agent(vehicle_id)
                self._task_ids.pop(vehicle_id, None)
                self._task_status[vehicle_id] = "assigned"
                self._emit(
                    "task_preempted",
                    {
                        "vehicle_id": vehicle_id,
                        "preempted_task_id": current_task_id,
                        "preempting_task_id": next_task_id,
                        "preempted_priority": current_task.priority,
                        "preempting_priority": next_task.priority,
                    },
                )
                print(
                    "任务抢占：{} 从 {} 切换到 {}（优先级 {} > {}）".format(
                        vehicle_id,
                        current_task_id,
                        next_task_id,
                        next_task.priority,
                        current_task.priority,
                    )
                )
            elif current_task_id:
                self._release_agent(vehicle_id)
                self._task_ids.pop(vehicle_id, None)
                self._task_status[vehicle_id] = "idle"
            self._start_next_task(vehicle_id)

    def tick(self, timeout_seconds: float = 2.0) -> Dict[str, str]:
        self._require_connected()
        for vehicle_id, agent in list(self._agents.items()):
            actor = self._actors[vehicle_id]
            if vehicle_id in self._faulted:
                continue
            if (
                vehicle_id in self._paused
                or vehicle_id in self._emergency_stopped
            ):
                self._stop_vehicle(vehicle_id, hand_brake=True)
                continue
            task_id = self._task_ids.get(vehicle_id)
            task = self._task_objects.get(task_id) if task_id else None
            target = self._task_targets.get(task_id) if task_id else None
            location = actor.get_location()
            distance = None
            if target is not None:
                distance = Position(
                    location.x, location.y, location.z
                ).distance_to(target)
                if task is not None:
                    task.last_distance_m = round(distance, 3)
            arrived = (
                distance is not None
                and distance <= self.config.demo.arrival_tolerance_m
            )
            started_tick = (
                self._task_started_ticks.get(task_id, self._tick_index)
                if task_id
                else self._tick_index
            )
            timed_out = (
                task is not None
                and self._tick_index - started_tick
                >= self.config.demo.task_timeout_ticks
            )
            if arrived:
                actor.apply_control(
                    self.carla.VehicleControl(
                        throttle=0.0, brake=1.0, hand_brake=False
                    )
                )
                completed_task_id = self._task_ids.pop(vehicle_id, None)
                if completed_task_id:
                    completed_task = self._task_objects.get(completed_task_id)
                    if completed_task is not None:
                        now = utc_now()
                        completed_task.status = "completed"
                        completed_task.updated_at = now
                        completed_task.completed_at = now
                        completed_task.completed_tick = self._tick_index
                        completed_task.status_reason = "arrival_tolerance"
                        self._vehicles_with_completed_task.add(
                            vehicle_id
                        )
                    self._emit(
                        "task_completed",
                        {
                            "vehicle_id": vehicle_id,
                            "task_id": completed_task_id,
                            "completion_reason": "arrival_tolerance",
                            "distance_m": (
                                round(distance, 3)
                                if distance is not None
                                else None
                            ),
                        },
                    )
                    queue = self._task_queues.get(vehicle_id, [])
                    self._task_queues[vehicle_id] = [
                        task_id
                        for task_id in queue
                        if task_id != completed_task_id
                    ]
                    self._task_targets.pop(completed_task_id, None)
                    self._task_started_ticks.pop(completed_task_id, None)
                self._release_agent(vehicle_id)
                self._task_status[vehicle_id] = "completed"
                self._start_next_task(vehicle_id)
            elif timed_out:
                actor.apply_control(
                    self.carla.VehicleControl(
                        throttle=0.0, brake=1.0, hand_brake=False
                    )
                )
                timed_out_task_id = self._task_ids.pop(vehicle_id, None)
                if timed_out_task_id:
                    timed_out_task = self._task_objects.get(timed_out_task_id)
                    if timed_out_task is not None:
                        now = utc_now()
                        timed_out_task.status = "timed_out"
                        timed_out_task.updated_at = now
                        timed_out_task.completed_at = now
                        timed_out_task.completed_tick = self._tick_index
                        timed_out_task.status_reason = "task_timeout"
                    self._emit(
                        "task_timed_out",
                        {
                            "vehicle_id": vehicle_id,
                            "task_id": timed_out_task_id,
                            "distance_m": (
                                round(distance, 3)
                                if distance is not None
                                else None
                            ),
                            "timeout_ticks": self.config.demo.task_timeout_ticks,
                        },
                    )
                    queue = self._task_queues.get(vehicle_id, [])
                    self._task_queues[vehicle_id] = [
                        queued_task_id
                        for queued_task_id in queue
                        if queued_task_id != timed_out_task_id
                    ]
                    self._task_targets.pop(timed_out_task_id, None)
                    self._task_started_ticks.pop(timed_out_task_id, None)
                self._release_agent(vehicle_id)
                self._task_status[vehicle_id] = "timed_out"
                self._start_next_task(vehicle_id)
            else:
                actor.apply_control(agent.run_step())
        self._update_spectator_camera()
        try:
            self.world.wait_for_tick(timeout_seconds)
        except RuntimeError as exc:
            raise CarlaAdapterError("CARLA tick timeout/error: {}".format(exc)) from exc
        self._tick_index += 1
        return dict(self._task_status)

    def inject_fault(self, vehicle_id: str) -> None:
        self._require_connected()
        actor = self._actors.get(vehicle_id)
        if actor is None:
            raise CarlaAdapterError("Unknown CARLA vehicle: {}".format(vehicle_id))
        original_location = actor.get_location()
        original_position = {
            "x": original_location.x,
            "y": original_location.y,
            "z": original_location.z,
        }
        pull_over_offset = self.config.demo.fault_pull_over_offset_m
        safe_stop_target_position = dict(original_position)
        if pull_over_offset > 0:
            transform = actor.get_transform()
            right_vector = transform.get_right_vector()
            transform.location.x += right_vector.x * pull_over_offset
            transform.location.y += right_vector.y * pull_over_offset
            transform.location.z += right_vector.z * pull_over_offset
            safe_stop_target_position = {
                "x": transform.location.x,
                "y": transform.location.y,
                "z": transform.location.z,
            }
            actor.set_transform(transform)
        self._paused.discard(vehicle_id)
        self._emergency_stopped.discard(vehicle_id)
        self._pause_started_ticks.pop(vehicle_id, None)
        self._faulted.add(vehicle_id)
        current_task_id = self._task_ids.get(vehicle_id)
        self._release_agent(vehicle_id)
        self._task_ids.pop(vehicle_id, None)
        self._task_status[vehicle_id] = "fault"
        actor.apply_control(
            self.carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True)
        )
        if pull_over_offset > 0:
            actor.set_simulate_physics(False)
        self._emit(
            "vehicle_fault_applied",
            {
                "vehicle_id": vehicle_id,
                "interrupted_task_id": current_task_id,
                "fault_mode": (
                    "safe_pull_over_then_disable"
                    if pull_over_offset > 0
                    else "in_place_disable"
                ),
                "pull_over_offset_m": pull_over_offset,
                "original_position": original_position,
                "safe_stop_target_position": safe_stop_target_position,
                "physics_disabled": pull_over_offset > 0,
            },
        )

    def pause_vehicle(self, vehicle_id: str) -> Dict[str, object]:
        self._require_connected()
        actor = self._actors.get(vehicle_id)
        if actor is None:
            raise CarlaAdapterError(
                "Unknown CARLA vehicle: {}".format(vehicle_id)
            )
        if vehicle_id in self._faulted:
            raise CarlaAdapterError(
                "故障车辆不能执行暂停：{}".format(vehicle_id)
            )

        if vehicle_id not in self._paused:
            self._pause_started_ticks[vehicle_id] = self._tick_index
        self._emergency_stopped.discard(vehicle_id)
        self._paused.add(vehicle_id)
        self._task_status[vehicle_id] = "paused"
        self._stop_vehicle(vehicle_id, hand_brake=True)
        self._emit(
            "vehicle_paused_by_command",
            {
                "vehicle_id": vehicle_id,
                "task_id": self._task_ids.get(vehicle_id),
            },
        )
        return {
            "vehicle_id": vehicle_id,
            "status": "paused",
            "task_id": self._task_ids.get(vehicle_id),
        }

    def resume_vehicle(self, vehicle_id: str) -> Dict[str, object]:
        self._require_connected()
        actor = self._actors.get(vehicle_id)
        if actor is None:
            raise CarlaAdapterError(
                "Unknown CARLA vehicle: {}".format(vehicle_id)
            )
        if vehicle_id in self._faulted:
            raise CarlaAdapterError(
                "故障车辆不能直接恢复：{}".format(vehicle_id)
            )

        was_stopped = (
            vehicle_id in self._paused
            or vehicle_id in self._emergency_stopped
        )
        paused_tick = self._pause_started_ticks.pop(
            vehicle_id, self._tick_index
        )
        self._paused.discard(vehicle_id)
        self._emergency_stopped.discard(vehicle_id)

        task_id = self._task_ids.get(vehicle_id)
        if task_id is not None and was_stopped:
            pause_duration = max(0, self._tick_index - paused_tick)
            if task_id in self._task_started_ticks:
                self._task_started_ticks[task_id] += pause_duration

        actor.apply_control(
            self.carla.VehicleControl(
                throttle=0.0,
                brake=0.0,
                hand_brake=False,
            )
        )

        if task_id is not None and vehicle_id in self._agents:
            self._task_status[vehicle_id] = "executing"
        else:
            self._task_status[vehicle_id] = "idle"
            self._start_next_task(vehicle_id)

        self._emit(
            "vehicle_resumed_by_command",
            {
                "vehicle_id": vehicle_id,
                "task_id": self._task_ids.get(vehicle_id),
            },
        )
        return {
            "vehicle_id": vehicle_id,
            "status": self._task_status.get(vehicle_id, "idle"),
            "task_id": self._task_ids.get(vehicle_id),
        }

    def emergency_stop_vehicle(
        self, vehicle_id: str
    ) -> Dict[str, object]:
        self._require_connected()
        actor = self._actors.get(vehicle_id)
        if actor is None:
            raise CarlaAdapterError(
                "Unknown CARLA vehicle: {}".format(vehicle_id)
            )
        if vehicle_id in self._faulted:
            raise CarlaAdapterError(
                "车辆已经处于故障状态：{}".format(vehicle_id)
            )

        if vehicle_id not in self._emergency_stopped:
            self._pause_started_ticks[vehicle_id] = self._tick_index
        self._paused.discard(vehicle_id)
        self._emergency_stopped.add(vehicle_id)
        self._task_status[vehicle_id] = "emergency_stop"
        self._stop_vehicle(vehicle_id, hand_brake=True)
        self._emit(
            "vehicle_emergency_stopped_by_command",
            {
                "vehicle_id": vehicle_id,
                "task_id": self._task_ids.get(vehicle_id),
            },
        )
        return {
            "vehicle_id": vehicle_id,
            "status": "emergency_stop",
            "task_id": self._task_ids.get(vehicle_id),
        }

    def reassign_task(
        self,
        task_id: str,
        vehicle_id: str,
        speed_limit_kmh: Optional[float] = None,
    ) -> Dict[str, object]:
        """执行人工强制改派，并让目标车辆立即重新规划。"""

        self._require_connected()
        if vehicle_id not in self._actors:
            raise CarlaAdapterError(
                "Unknown CARLA vehicle: {}".format(vehicle_id)
            )
        if vehicle_id in self._faulted:
            raise CarlaAdapterError(
                "不能把任务分配给故障车辆：{}".format(vehicle_id)
            )

        task = self._task_objects.get(task_id)
        if task is None:
            raise CarlaAdapterError(
                "Unknown task: {}".format(task_id)
            )
        if task.status in {"completed", "timed_out", "cancelled"}:
            raise CarlaAdapterError(
                "终态任务不能重新分配：{} ({})".format(
                    task_id, task.status
                )
            )

        old_vehicle_id = task.assigned_vehicle_id

        for queued_vehicle_id, queue in self._task_queues.items():
            self._task_queues[queued_vehicle_id] = [
                queued_task_id
                for queued_task_id in queue
                if queued_task_id != task_id
            ]

        # 释放正在执行该任务的旧车辆。
        if (
            old_vehicle_id
            and self._task_ids.get(old_vehicle_id) == task_id
        ):
            self._release_agent(old_vehicle_id)
            self._task_ids.pop(old_vehicle_id, None)
            self._task_status[old_vehicle_id] = "idle"

        # 人工改派具有抢占权：目标车辆当前任务退回assigned。
        current_target_task_id = self._task_ids.get(vehicle_id)
        if current_target_task_id and current_target_task_id != task_id:
            current_target_task = self._task_objects.get(
                current_target_task_id
            )
            if current_target_task is not None:
                current_target_task.status = "assigned"
                current_target_task.updated_at = utc_now()
                current_target_task.started_at = None
                current_target_task.started_tick = None
                current_target_task.status_reason = (
                    "preempted_by_human_dispatch"
                )
            self._release_agent(vehicle_id)
            self._task_ids.pop(vehicle_id, None)

        now = utc_now()
        task.assigned_vehicle_id = vehicle_id
        task.status = "assigned"
        task.updated_at = now
        task.started_at = None
        task.completed_at = None
        task.started_tick = None
        task.completed_tick = None
        task.status_reason = "assigned_by_human_operator"

        target_queue = self._task_queues.setdefault(vehicle_id, [])
        target_queue.insert(0, task_id)
        self._task_status[vehicle_id] = "assigned"
        self._start_next_task(vehicle_id)

        agent = self._agents.get(vehicle_id)
        if speed_limit_kmh is not None and agent is not None:
            setter = getattr(agent, "set_target_speed", None)
            if callable(setter):
                setter(float(speed_limit_kmh))

        if old_vehicle_id and old_vehicle_id != vehicle_id:
            self._start_next_task(old_vehicle_id)

        self._emit(
            "task_reassigned_by_human",
            {
                "task_id": task_id,
                "old_vehicle_id": old_vehicle_id,
                "new_vehicle_id": vehicle_id,
                "speed_limit_kmh": speed_limit_kmh,
            },
        )
        return {
            "task_id": task_id,
            "old_vehicle_id": old_vehicle_id,
            "vehicle_id": vehicle_id,
            "status": self._task_status.get(vehicle_id, "assigned"),
        }

    def close(self) -> None:
        for vehicle_id in list(self._agents):
            self._stop_vehicle(
                vehicle_id, hand_brake=True
            )
            self._release_agent(vehicle_id)
        self._task_queues.clear()
        self._task_objects.clear()
        self._zones_by_id.clear()
        self._task_targets.clear()
        self._task_started_ticks.clear()
        self._spectator = None
        self._camera_vehicle_id = None
        self._trajectory_points.clear()
        self._paused.clear()
        self._emergency_stopped.clear()
        self._pause_started_ticks.clear()
        self._road_segments_cache = None
        self._map_bounds_cache = None
        self._parked.clear()
        self._vehicles_with_completed_task.clear()
        self.client = None
        self.world = None

    def drain_events(self) -> List[Dict[str, object]]:
        events = list(self._events)
        self._events.clear()
        return events

    def _import_carla(self) -> None:
        root = Path(self.config.carla.root).expanduser().resolve()
        dist = root / "PythonAPI" / "carla" / "dist"
        egg_pattern = str(dist / "carla-*-py3.7-linux-x86_64.egg")
        eggs = sorted(glob.glob(egg_pattern))
        if not eggs:
            raise CarlaAdapterError(
                "CARLA Python 3.7 egg not found under {}".format(dist)
            )
        agents_root = root / "PythonAPI" / "carla"
        for entry in (eggs[-1], str(agents_root)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        try:
            import carla
            from agents.navigation.basic_agent import BasicAgent
        except ImportError as exc:
            raise CarlaAdapterError(
                "Failed to import CARLA API/BasicAgent: {}".format(exc)
            ) from exc
        self.carla = carla
        self._basic_agent_class = BasicAgent

    def _discover_configured_vehicles(self) -> None:
        role_to_id = {
            item.role_name: item.vehicle_id for item in self.config.vehicles
        }
        self._actors.clear()
        for actor in self.world.get_actors().filter("vehicle.*"):
            role_name = actor.attributes.get("role_name", "")
            vehicle_id = role_to_id.get(role_name)
            if vehicle_id and vehicle_id not in self._actors:
                self._actors[vehicle_id] = actor

    def _get_blueprint(self, definition: VehicleConfig):
        library = self.world.get_blueprint_library()
        try:
            return library.find(definition.blueprint)
        except RuntimeError:
            candidates = list(library.filter("vehicle.*"))
            four_wheel = [
                item
                for item in candidates
                if item.has_attribute("number_of_wheels")
                and int(item.get_attribute("number_of_wheels")) == 4
            ]
            if not four_wheel:
                raise CarlaAdapterError(
                    "Blueprint {} is unavailable and no four-wheel fallback exists".format(
                        definition.blueprint
                    )
                )
            return sorted(four_wheel, key=lambda item: item.id)[0]

    def _start_next_task(self, vehicle_id: str) -> None:
        if (
            vehicle_id in self._faulted
            or vehicle_id in self._paused
            or vehicle_id in self._emergency_stopped
            or vehicle_id in self._agents
        ):
            return
        queue = self._task_queues.get(vehicle_id, [])
        while queue:
            task_id = queue[0]
            task = self._task_objects.get(task_id)
            if task is None or task.status in {
                "completed",
                "timed_out",
                "cancelled",
            }:
                queue.pop(0)
                continue
            actor = self._actors.get(vehicle_id)
            if actor is None:
                raise CarlaAdapterError(
                    "Configured vehicle is not present in CARLA: {}".format(vehicle_id)
                )
            zone = self._zones_by_id[task.zone_id]
            spawn_points = self.world.get_map().get_spawn_points()
            target_index = zone.target_spawn_point_index % len(spawn_points)
            target_location = spawn_points[target_index].location
            target = Position(
                target_location.x, target_location.y, target_location.z
            )
            definition = next(
                item
                for item in self.config.vehicles
                if item.vehicle_id == vehicle_id
            )
            agent = self._basic_agent_class(
                actor, target_speed=definition.target_speed_kmh
            )
            self._parked.discard(vehicle_id)
            actor.apply_control(
                self.carla.VehicleControl(
                    throttle=0.0,
                    brake=0.0,
                    hand_brake=False,
                )
            )
            agent.set_destination(
                [target_location.x, target_location.y, target_location.z]
            )
            task.status = "executing"
            now = utc_now()
            task.updated_at = now
            task.started_at = now
            task.started_tick = self._tick_index
            task.completed_at = None
            task.completed_tick = None
            task.attempt_count += 1
            task.last_distance_m = round(
                Position(
                    actor.get_location().x,
                    actor.get_location().y,
                    actor.get_location().z,
                ).distance_to(target),
                3,
            )
            task.status_reason = "navigation_started"
            self._task_targets[task.task_id] = target
            self._task_started_ticks[task.task_id] = self._tick_index
            self._agents[vehicle_id] = agent
            self._task_ids[vehicle_id] = task.task_id
            self._task_status[vehicle_id] = "executing"
            self._emit(
                "task_started",
                {
                    "vehicle_id": vehicle_id,
                    "task_id": task.task_id,
                    "attempt_count": task.attempt_count,
                    "initial_distance_m": task.last_distance_m,
                    "target_spawn_point_index": target_index,
                    "target_position": {
                        "x": target.x,
                        "y": target.y,
                        "z": target.z,
                    },
                },
            )
            return
        self._task_status[vehicle_id] = "idle"
        self._park_vehicle_off_route(vehicle_id)
        self._stop_vehicle(vehicle_id, hand_brake=True)

    def _park_vehicle_off_route(self, vehicle_id: str) -> None:
        offset = self.config.demo.idle_pull_over_offset_m
        actor = self._actors.get(vehicle_id)
        if (
            offset <= 0
            or actor is None
            or vehicle_id in self._parked
            or vehicle_id not in self._vehicles_with_completed_task
        ):
            return
        original = actor.get_location()
        original_position = {
            "x": original.x,
            "y": original.y,
            "z": original.z,
        }
        transform = actor.get_transform()
        right_vector = transform.get_right_vector()
        transform.location.x += right_vector.x * offset
        transform.location.y += right_vector.y * offset
        transform.location.z += right_vector.z * offset
        actor.set_transform(transform)
        self._parked.add(vehicle_id)
        self._emit(
            "vehicle_parked_off_route",
            {
                "vehicle_id": vehicle_id,
                "offset_m": offset,
                "original_position": original_position,
                "parking_position": {
                    "x": transform.location.x,
                    "y": transform.location.y,
                    "z": transform.location.z,
                },
                "reason": "task_queue_empty_release_shared_lane",
            },
        )

    def _stop_vehicle(
        self, vehicle_id: str, hand_brake: bool
    ) -> None:
        actor = self._actors.get(vehicle_id)
        if actor is None or self.carla is None:
            return
        actor.apply_control(
            self.carla.VehicleControl(
                throttle=0.0,
                brake=1.0,
                hand_brake=hand_brake,
            )
        )

    def _update_spectator_camera(self) -> None:
        if self._spectator is None or self.carla is None:
            return
        candidates = []
        for vehicle_id, task_id in self._task_ids.items():
            task = self._task_objects.get(task_id)
            actor = self._actors.get(vehicle_id)
            if task is None or actor is None:
                continue
            candidates.append(
                (task.priority, vehicle_id, actor, task)
            )
        if not candidates:
            return
        _, vehicle_id, actor, task = max(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        actor_transform = actor.get_transform()
        forward = actor_transform.get_forward_vector()
        actor_location = actor_transform.location
        camera_location = self.carla.Location(
            x=actor_location.x - forward.x * 9.0,
            y=actor_location.y - forward.y * 9.0,
            z=actor_location.z + 5.0,
        )
        actor_rotation = actor_transform.rotation
        camera_rotation = self.carla.Rotation(
            pitch=-18.0,
            yaw=actor_rotation.yaw,
            roll=0.0,
        )
        self._spectator.set_transform(
            self.carla.Transform(
                camera_location, camera_rotation
            )
        )
        if self._camera_vehicle_id != vehicle_id:
            self._camera_vehicle_id = vehicle_id
            self._emit(
                "spectator_camera_target_changed",
                {
                    "vehicle_id": vehicle_id,
                    "task_id": task.task_id,
                    "camera_mode": (
                        "priority_vehicle_chase"
                    ),
                },
            )

    def _release_agent(self, vehicle_id: str) -> None:
        """Detach CARLA 0.9.10 LocalPlanner before disposal.

        In CARLA 0.9.10 LocalPlanner.__del__ destroys its vehicle unless
        reset_vehicle() has cleared the reference first. The third-party
        scheduler must release controllers without taking ownership of the
        simulated equipment lifecycle.
        """

        agent = self._agents.get(vehicle_id)
        if agent is None:
            return
        local_planner = getattr(agent, "_local_planner", None)
        reset_vehicle = getattr(local_planner, "reset_vehicle", None)
        if callable(reset_vehicle):
            reset_vehicle()
        self._agents.pop(vehicle_id, None)

    def _emit(self, event_type: str, payload: Dict[str, object]) -> None:
        self._events.append(
            {
                "event_type": event_type,
                "tick": self._tick_index,
                "payload": payload,
            }
        )

    def _require_connected(self) -> None:
        if self.world is None:
            raise CarlaAdapterError("CarlaAdapter is not connected")
