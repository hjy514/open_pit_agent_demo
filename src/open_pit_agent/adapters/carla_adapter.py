"""CARLA 0.9.10 adapter kept separate from scheduling business logic."""

import glob
import json
import math
import os
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
        self._global_route_planner_class = None
        self._global_route_planner_dao_class = None
        self._actors: Dict[str, object] = {}
        self._agents: Dict[str, object] = {}
        self._task_ids: Dict[str, str] = {}
        self._task_status: Dict[str, str] = {}
        self._task_objects: Dict[str, Task] = {}
        self._task_queues: Dict[str, List[str]] = {}
        self._zones_by_id: Dict[str, ZoneConfig] = {}
        self._task_targets: Dict[str, Position] = {}
        self._route_remaining_targets: Dict[str, List[Position]] = {}
        self._production_plans: Dict[str, Dict[str, object]] = {}
        self._production_holds: Dict[str, Dict[str, object]] = {}
        self._mission_plans: Dict[str, Dict[str, object]] = {}
        self._takeover_routes: Dict[str, List[Position]] = {}
        self._task_speed_limits: Dict[str, float] = {}
        self._task_arrival_tolerances: Dict[str, float] = {}
        self._takeover_task_ids = set()
        self._safe_route_plan: Optional[Dict[str, object]] = None
        self._hazard_replanned_task_ids = set()
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
        self._camera_sensors: Dict[str, object] = {}
        self._camera_display_names: Dict[str, str] = {}
        self._camera_frame_numbers: Dict[str, int] = {}
        self._camera_output_dir = (
            Path(__file__).resolve().parents[3]
            / "artifacts"
            / "live_cameras"
        )
        self._trajectory_points: Dict[str, List[Dict[str, float]]] = {}
        self._road_segments_cache: Optional[List[Dict[str, object]]] = None
        self._map_bounds_cache: Optional[Dict[str, float]] = None
        self.spawned_actor_ids: List[int] = []

    def configure_task_missions(
        self, plans: Sequence[Dict[str, object]]
    ) -> Dict[str, object]:
        """Configure spawn -> service origin -> service target execution.

        The task remains one business object.  Deadhead travel is an explicit
        pre-service stage and therefore cannot be mistaken for task
        completion when the truck only reaches its loading/patrol origin.
        """
        self._require_connected()
        spawn_points = self.world.get_map().get_spawn_points()
        configured = []
        deadhead_count = 0
        for raw in plans:
            if not isinstance(raw, dict) or not raw.get("task_id"):
                continue
            origin_index = int(raw["service_origin_spawn_point_index"])
            target_index = int(raw["service_target_spawn_point_index"])
            if not (0 <= origin_index < len(spawn_points)):
                raise CarlaAdapterError(
                    "Mission service origin out of range: {}".format(origin_index)
                )
            if not (0 <= target_index < len(spawn_points)):
                raise CarlaAdapterError(
                    "Mission service target out of range: {}".format(target_index)
                )
            origin = spawn_points[origin_index].location
            target = spawn_points[target_index].location
            task_id = str(raw["task_id"])
            requires_deadhead = bool(raw.get("requires_deadhead"))
            self._mission_plans[task_id] = {
                **dict(raw),
                "service_origin": Position(origin.x, origin.y, origin.z),
                "service_target": Position(target.x, target.y, target.z),
                "phase": (
                    "pending_deadhead" if requires_deadhead
                    else "at_service_origin"
                ),
            }
            configured.append(task_id)
            deadhead_count += int(requires_deadhead)
        return {
            "status": "CONFIGURED",
            "task_count": len(configured),
            "deadhead_task_count": deadhead_count,
            "task_ids": configured,
            "execution_model": "SPAWN_TO_SERVICE_ORIGIN_TO_SERVICE_TARGET",
        }

    def configure_production_cycles(
        self, plans: Sequence[Dict[str, object]]
    ) -> Dict[str, object]:
        """Configure map-anchored haul service execution for tasks.

        New plans may complete after dumping because a task represents one
        directed mission.  The legacy return behaviour remains the default for
        callers that do not provide ``completion_after_dumping``.
        """
        self._require_connected()
        spawn_points = self.world.get_map().get_spawn_points()
        configured = []
        for raw in plans:
            if not isinstance(raw, dict) or not raw.get("task_id"):
                continue
            origin_index = int(raw["origin_spawn_point_index"])
            if origin_index < 0 or origin_index >= len(spawn_points):
                raise CarlaAdapterError(
                    "Production origin spawn point out of range: {}".format(
                        origin_index
                    )
                )
            location = spawn_points[origin_index].location
            task_id = str(raw["task_id"])
            self._production_plans[task_id] = {
                "task_id": task_id,
                "origin_spawn_point_index": origin_index,
                "return_target": Position(location.x, location.y, location.z),
                "loading_ticks": max(0, int(raw.get("loading_ticks", 0))),
                "dumping_ticks": max(0, int(raw.get("dumping_ticks", 0))),
                "phase": "pending_loading",
                "return_route_evidence": raw.get("return_route_evidence"),
                "completion_after_dumping": bool(
                    raw.get("completion_after_dumping", False)
                ),
                "task_completion_semantics": raw.get(
                    "task_completion_semantics", "legacy_round_trip"
                ),
            }
            configured.append(task_id)
        return {
            "status": "CONFIGURED",
            "task_count": len(configured),
            "task_ids": configured,
            "service_model": "CARLA_STATIONARY_SERVICE_HOLD",
            "completion_model": "PER_TASK_DIRECTED_MISSION",
        }

    def _begin_production_leg(
        self, vehicle_id: str, task_id: str, target: Position,
        phase: str, reason: str,
    ) -> None:
        task = self._task_objects[task_id]
        plan = self._production_plans[task_id]
        plan["phase"] = phase
        self._task_targets[task_id] = target
        self._task_ids[vehicle_id] = task_id
        self._task_status[vehicle_id] = phase
        task.status = "executing"
        task.status_reason = reason
        self._start_navigation_leg(vehicle_id, task_id, target)
        self._emit("task_production_stage_changed", {
            "vehicle_id": vehicle_id, "task_id": task_id,
            "to_status": phase, "reason": reason,
        })

    def _begin_loading_hold(
        self, vehicle_id: str, task: Task,
        production_plan: Dict[str, object], outbound_target: Position,
    ) -> None:
        """Enter the loading service stage at the admitted service origin."""
        now = utc_now()
        task.status = "executing"
        task.updated_at = now
        if task.started_at is None:
            task.started_at = now
            task.started_tick = self._tick_index
            task.attempt_count += 1
        task.completed_at = None
        task.completed_tick = None
        task.status_reason = "loading_service_hold"
        self._task_targets[task.task_id] = outbound_target
        self._task_started_ticks[task.task_id] = self._tick_index
        self._task_ids[vehicle_id] = task.task_id
        self._task_status[vehicle_id] = "loading"
        production_plan["phase"] = "loading"
        loading_ticks = int(production_plan.get("loading_ticks") or 0)
        self._production_holds[vehicle_id] = {
            "task_id": task.task_id,
            "until_tick": self._tick_index + loading_ticks,
            "next_target": outbound_target,
            "next_phase": "loaded_haul",
            "reason": "loading_completed_loaded_haul_started",
        }
        self._stop_vehicle(vehicle_id, hand_brake=True)
        self._emit("task_production_stage_changed", {
            "vehicle_id": vehicle_id,
            "task_id": task.task_id,
            "from_status": "queue_loader",
            "to_status": "loading",
            "reason": "loader_capacity_available",
            "service_ticks": loading_ticks,
        })

    def _advance_production_holds(self) -> None:
        for vehicle_id, hold in list(self._production_holds.items()):
            if vehicle_id in self._faulted or vehicle_id in self._paused:
                continue
            if self._tick_index < int(hold["until_tick"]):
                self._stop_vehicle(vehicle_id, hand_brake=True)
                continue
            self._production_holds.pop(vehicle_id, None)
            if hold.get("complete_task"):
                self._complete_task(
                    vehicle_id,
                    str(hold["task_id"]),
                    completion_reason=str(
                        hold.get("reason") or "destination_service_completed"
                    ),
                )
                continue
            self._begin_production_leg(
                vehicle_id, str(hold["task_id"]), hold["next_target"],
                str(hold["next_phase"]), str(hold["reason"]),
            )

    def _complete_task(
        self, vehicle_id: str, task_id: str,
        completion_reason: str = "arrival_tolerance",
        distance_m: Optional[float] = None,
    ) -> None:
        """Finish one task and consistently release all execution state."""
        active_task_id = self._task_ids.pop(vehicle_id, None)
        completed_task_id = active_task_id or task_id
        completed_task = self._task_objects.get(completed_task_id)
        production_plan = self._production_plans.get(completed_task_id)
        if completed_task is not None:
            now = utc_now()
            completed_task.status = "completed"
            completed_task.updated_at = now
            completed_task.completed_at = now
            completed_task.completed_tick = self._tick_index
            completed_task.status_reason = completion_reason
            self._vehicles_with_completed_task.add(vehicle_id)
        self._emit("task_completed", {
            "vehicle_id": vehicle_id,
            "task_id": completed_task_id,
            "completion_reason": completion_reason,
            "distance_m": (
                round(distance_m, 3) if distance_m is not None else None
            ),
        })
        if production_plan:
            self._emit("task_production_stage_changed", {
                "vehicle_id": vehicle_id,
                "task_id": completed_task_id,
                "from_status": production_plan.get("phase"),
                "to_status": "terminal",
                "reason": completion_reason,
                "return_route_evidence": production_plan.get(
                    "return_route_evidence"
                ),
            })
        mission_plan = self._mission_plans.get(completed_task_id)
        if mission_plan:
            previous_phase = mission_plan.get("phase")
            mission_plan["phase"] = "terminal"
            self._emit("task_mission_stage_changed", {
                "vehicle_id": vehicle_id,
                "task_id": completed_task_id,
                "from_status": previous_phase,
                "to_status": "terminal",
                "reason": completion_reason,
            })
        if completed_task_id in self._hazard_replanned_task_ids:
            self._emit("hazard_route_replan_completed", {
                "vehicle_id": vehicle_id,
                "task_id": completed_task_id,
                "route_plan_id": (self._safe_route_plan or {}).get(
                    "route_plan_id"
                ),
            })
            self._hazard_replanned_task_ids.discard(completed_task_id)
        queue = self._task_queues.get(vehicle_id, [])
        self._task_queues[vehicle_id] = [
            queued_task_id for queued_task_id in queue
            if queued_task_id != completed_task_id
        ]
        self._task_targets.pop(completed_task_id, None)
        self._route_remaining_targets.pop(completed_task_id, None)
        self._takeover_routes.pop(completed_task_id, None)
        self._task_speed_limits.pop(completed_task_id, None)
        self._task_arrival_tolerances.pop(completed_task_id, None)
        self._takeover_task_ids.discard(completed_task_id)
        self._task_started_ticks.pop(completed_task_id, None)
        self._production_holds.pop(vehicle_id, None)
        self._release_agent(vehicle_id)
        self._task_status[vehicle_id] = "completed"
        self._start_next_task(vehicle_id)
        if (
            vehicle_id not in self._task_ids
            and not self._all_tasks_terminal()
        ):
            self.retire_vehicle(
                vehicle_id,
                reason="completed_vehicle_clears_active_routes",
            )

    def configure_safe_route(
        self,
        route_plan_id: str,
        waypoint_spawn_point_indices: Sequence[int],
        task_types: Sequence[str],
        blocked_road_segment_id: str,
    ) -> Dict[str, object]:
        """Route affected tasks through configured safety waypoints.

        CARLA 0.9.10's BasicAgent accepts one destination at a time.  The
        adapter therefore executes the route as real navigation legs:
        current position -> B1 -> ... -> final task destination.
        """

        self._require_connected()
        spawn_points = self.world.get_map().get_spawn_points()
        if not spawn_points:
            raise CarlaAdapterError("Current map has no vehicle spawn points")
        waypoint_indices = [
            int(index) % len(spawn_points)
            for index in waypoint_spawn_point_indices
        ]
        if not waypoint_indices:
            raise CarlaAdapterError(
                "Safe route requires at least one waypoint"
            )
        waypoint_positions = []
        for waypoint_index in waypoint_indices:
            location = spawn_points[waypoint_index].location
            waypoint_positions.append(
                Position(location.x, location.y, location.z)
            )
        self._safe_route_plan = {
            "route_plan_id": str(route_plan_id),
            "waypoint_spawn_point_indices": waypoint_indices,
            "waypoint_positions": waypoint_positions,
            "task_types": {str(value) for value in task_types},
            "blocked_road_segment_id": str(blocked_road_segment_id),
            "strategy": "carla_basic_agent_via_safe_waypoint",
        }
        replanned_task_ids = []
        waypoints = self._safe_route_plan["waypoint_positions"]
        safe_task_types = self._safe_route_plan["task_types"]
        for vehicle_id, task_id in list(self._task_ids.items()):
            task = self._task_objects.get(task_id)
            original_target = self._task_targets.get(task_id)
            if (
                task is None
                or task.task_type not in safe_task_types
                or original_target is None
                or not waypoints
            ):
                continue
            self._route_remaining_targets[task_id] = list(waypoints[1:]) + [
                original_target
            ]
            self._hazard_replanned_task_ids.add(task_id)
            self._emit(
                "hazard_route_replan_started",
                {
                    "vehicle_id": vehicle_id,
                    "task_id": task_id,
                    "route_plan_id": str(route_plan_id),
                    "blocked_road_segment_id": str(blocked_road_segment_id),
                    "waypoint_spawn_point_indices": waypoint_indices,
                },
            )
            self._release_agent(vehicle_id)
            self._start_navigation_leg(vehicle_id, task_id, waypoints[0])
            task.status_reason = "safe_route_replanned_during_execution"
            replanned_task_ids.append(task_id)
        def _json_value(value):
            if isinstance(value, set):
                return sorted(value)
            if isinstance(value, Position):
                return {"x": value.x, "y": value.y, "z": value.z}
            if isinstance(value, (list, tuple)):
                return [_json_value(item) for item in value]
            return value

        payload = {
            key: _json_value(value)
            for key, value in self._safe_route_plan.items()
        }
        payload["replanned_task_ids"] = replanned_task_ids
        self._emit("safe_route_activated", payload)
        self._emit("hazard_route_replanned", payload)
        return payload

    def clear_safe_route(self, reason: str) -> Optional[Dict[str, object]]:
        """Return future tasks to normal routing after risk review closes."""

        if self._safe_route_plan is None:
            return None
        payload = {
            "route_plan_id": self._safe_route_plan.get("route_plan_id"),
            "reason": str(reason),
        }
        self._safe_route_plan = None
        self._emit("safe_route_deactivated", payload)
        return payload

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
                ), file=sys.stderr
            )
        if spawned_count:
            try:
                self.world.wait_for_tick(self.config.carla.timeout_seconds)
            except RuntimeError as exc:
                raise CarlaAdapterError(
                    "Vehicles spawned, but CARLA did not produce a stabilization "
                    "tick: {}".format(exc)
                ) from exc
        self._ensure_camera_streams()

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
        try:
            if callable(get_plan):
                plan = list(get_plan())
            else:
                # CARLA 0.9.10 does not expose get_plan(), but keeps the
                # remaining global plan in this deque.
                plan = list(
                    getattr(local_planner, "_waypoints_queue", [])
                )
        except Exception:
            return []

        points: List[Dict[str, float]] = []
        for index, plan_item in enumerate(plan):
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

        for remaining_target in self._route_remaining_targets.get(
            target_task_id or "", []
        ):
            remaining_point = {
                "x": float(remaining_target.x),
                "y": float(remaining_target.y),
                "z": float(remaining_target.z),
            }
            if not points or math.hypot(
                remaining_point["x"] - points[-1]["x"],
                remaining_point["y"] - points[-1]["y"],
            ) > 1.0:
                points.append(remaining_point)

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
                self._suspend_active_task(
                    vehicle_id, current_task_id,
                    reason="preempted_by_higher_priority",
                )
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
                    ), file=sys.stderr
                )
            elif current_task_id:
                self._release_agent(vehicle_id)
                self._task_ids.pop(vehicle_id, None)
                self._route_remaining_targets.pop(current_task_id, None)
                self._task_status[vehicle_id] = "idle"
            self._start_next_task(vehicle_id)

    def tick(self, timeout_seconds: float = 2.0) -> Dict[str, str]:
        self._require_connected()
        self._advance_production_holds()
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
            arrival_tolerance = self._task_arrival_tolerances.get(
                task_id or "", self.config.demo.arrival_tolerance_m
            )
            remaining_targets = self._route_remaining_targets.get(
                task_id or "", []
            )
            if remaining_targets and task_id not in self._takeover_task_ids:
                # RoadGraph geometry is an upper-level route description, not
                # a centimetre-accurate CARLA control trajectory.  Give only
                # intermediate graph checkpoints enough clearance for a full-
                # size mine truck; the final task target keeps its P6-backed
                # arrival tolerance below.
                arrival_tolerance = max(arrival_tolerance, 18.0)
            if task_id in self._takeover_task_ids and self._route_remaining_targets.get(
                task_id or ""
            ):
                arrival_tolerance = min(arrival_tolerance, 6.0)
            arrived = (
                distance is not None
                and (
                    distance <= arrival_tolerance
                    or (
                        callable(getattr(agent, "done", None))
                        and agent.done()
                        and distance <= max(arrival_tolerance, 15.0)
                    )
                )
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
                if task_id and remaining_targets:
                    next_target = remaining_targets.pop(0)
                    self._release_agent(vehicle_id)
                    self._start_navigation_leg(
                        vehicle_id, task_id, next_target
                    )
                    self._task_targets[task_id] = next_target
                    if task is not None:
                        task.status_reason = (
                            "takeover_route_checkpoint_reached"
                            if task_id in self._takeover_task_ids
                            else "safe_route_waypoint_reached"
                        )
                    waypoint_event = {
                        "vehicle_id": vehicle_id,
                        "task_id": task_id,
                        "route_plan_id": (
                            self._safe_route_plan or {}
                        ).get("route_plan_id"),
                        "next_target": {
                            "x": next_target.x,
                            "y": next_target.y,
                            "z": next_target.z,
                        },
                    }
                    self._emit("safe_route_waypoint_reached", waypoint_event)
                    self._emit("hazard_route_waypoint_reached", waypoint_event)
                    if task_id in self._takeover_task_ids:
                        self._emit(
                            "hazard_task_takeover_checkpoint_reached",
                            waypoint_event,
                        )
                    continue
                mission_plan = self._mission_plans.get(task_id or "")
                if (
                    mission_plan
                    and mission_plan.get("phase") == "to_service_origin"
                    and task is not None
                ):
                    self._release_agent(vehicle_id)
                    mission_plan["phase"] = "at_service_origin"
                    task.status_reason = "service_origin_reached"
                    self._emit("task_mission_stage_changed", {
                        "vehicle_id": vehicle_id,
                        "task_id": task_id,
                        "from_status": "to_service_origin",
                        "to_status": "at_service_origin",
                        "reason": "deadhead_service_origin_reached",
                        "service_origin_point_id": mission_plan.get(
                            "service_origin_point_id"
                        ),
                    })
                    production_plan = self._production_plans.get(task_id)
                    if (
                        production_plan
                        and production_plan.get("phase") == "pending_loading"
                    ):
                        self._begin_loading_hold(
                            vehicle_id, task, production_plan,
                            mission_plan["service_target"],
                        )
                    else:
                        service_target = mission_plan["service_target"]
                        mission_plan["phase"] = "service_execution"
                        task.status_reason = "service_route_started"
                        self._task_targets[task_id] = service_target
                        self._task_started_ticks[task_id] = self._tick_index
                        self._task_status[vehicle_id] = "executing"
                        self._start_navigation_leg(
                            vehicle_id, task_id, service_target
                        )
                        self._emit("task_mission_stage_changed", {
                            "vehicle_id": vehicle_id,
                            "task_id": task_id,
                            "from_status": "at_service_origin",
                            "to_status": "service_execution",
                            "reason": "service_route_started",
                            "service_target_point_id": mission_plan.get(
                                "service_target_point_id"
                            ),
                        })
                    continue
                production_plan = self._production_plans.get(task_id or "")
                if production_plan and production_plan.get("phase") == "loaded_haul":
                    self._release_agent(vehicle_id)
                    production_plan["phase"] = "dumping"
                    dumping_ticks = int(production_plan.get("dumping_ticks") or 0)
                    self._task_status[vehicle_id] = "dumping"
                    if task is not None:
                        task.status_reason = "dumping_service_hold"
                    if production_plan.get("completion_after_dumping"):
                        self._production_holds[vehicle_id] = {
                            "task_id": task_id,
                            "until_tick": self._tick_index + dumping_ticks,
                            "complete_task": True,
                            "reason": "destination_service_completed",
                        }
                    else:
                        self._production_holds[vehicle_id] = {
                            "task_id": task_id,
                            "until_tick": self._tick_index + dumping_ticks,
                            "next_target": production_plan["return_target"],
                            "next_phase": "returning",
                            "reason": "dumping_completed_return_started",
                        }
                    self._emit("task_production_stage_changed", {
                        "vehicle_id": vehicle_id, "task_id": task_id,
                        "from_status": "loaded_haul", "to_status": "dumping",
                        "reason": "outbound_destination_reached",
                        "service_ticks": dumping_ticks,
                    })
                    continue
                completion_reason = (
                    "production_cycle_return_completed"
                    if production_plan
                    and production_plan.get("phase") == "returning"
                    else "arrival_tolerance"
                )
                if task_id:
                    self._complete_task(
                        vehicle_id, task_id, completion_reason, distance
                    )
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
                    self._route_remaining_targets.pop(
                        timed_out_task_id, None
                    )
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
            retired_status = str(self._task_status.get(vehicle_id, ""))
            if retired_status.startswith("retired_"):
                return {
                    "vehicle_id": vehicle_id,
                    "status": "ALREADY_RETIRED",
                    "task_id": None,
                }
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
            retired_status = str(self._task_status.get(vehicle_id, ""))
            if retired_status.startswith("retired_"):
                return {
                    "vehicle_id": vehicle_id,
                    "status": "ALREADY_RETIRED",
                    "task_id": None,
                }
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
            hold = self._production_holds.get(vehicle_id)
            if hold is not None:
                hold["until_tick"] = int(hold["until_tick"]) + pause_duration

        actor.apply_control(
            self.carla.VehicleControl(
                throttle=0.0,
                brake=0.0,
                hand_brake=False,
            )
        )

        if task_id is not None and vehicle_id in self._production_holds:
            plan = self._production_plans.get(task_id, {})
            self._task_status[vehicle_id] = str(
                plan.get("phase") or "production_service_hold"
            )
        elif task_id is not None and vehicle_id in self._agents:
            # A BasicAgent created before a long headway/event hold may keep
            # stale local-planner state after the heavy truck is released
            # from its hand brake.  Rebuild that one controller from the
            # actor's current transform to the current leg target.  Remaining
            # route checkpoints stay in ``_route_remaining_targets`` and are
            # therefore not lost.
            target = self._task_targets.get(task_id)
            if was_stopped and target is not None:
                self._release_agent(vehicle_id)
                self._start_navigation_leg(vehicle_id, task_id, target)
                self._emit(
                    "vehicle_navigation_refreshed_after_resume",
                    {
                        "vehicle_id": vehicle_id,
                        "task_id": task_id,
                        "reason": "resume_from_pause_rebuilds_basic_agent",
                    },
                )
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

    def freeze_hazard_task_route(
        self, vehicle_id: str, clearance_m: float = 50.0
    ) -> Optional[Dict[str, object]]:
        """Freeze the affected truck's progress before releasing its task."""

        task_id = self._task_ids.get(vehicle_id)
        task = self._task_objects.get(task_id) if task_id else None
        actor = self._actors.get(vehicle_id)
        if task is None or actor is None:
            return None

        route = list(task.original_route)
        if not route:
            route = self._extract_route_points(vehicle_id)
            task.original_route = list(route)
        if not route:
            return None

        location = actor.get_location()
        closest_index = min(
            range(len(route)),
            key=lambda index: math.sqrt(
                (float(route[index]["x"]) - location.x) ** 2
                + (float(route[index]["y"]) - location.y) ** 2
                + (float(route[index].get("z", 0.0)) - location.z) ** 2
            ),
        )
        merge_index = closest_index
        travelled = 0.0
        for index in range(closest_index + 1, len(route)):
            previous = route[index - 1]
            current = route[index]
            travelled += math.sqrt(
                (float(current["x"]) - float(previous["x"])) ** 2
                + (float(current["y"]) - float(previous["y"])) ** 2
                + (
                    float(current.get("z", 0.0))
                    - float(previous.get("z", 0.0))
                ) ** 2
            )
            merge_index = index
            if travelled >= float(clearance_m):
                break

        task.completed_route = list(route[:closest_index])
        task.last_completed_waypoint = (
            dict(route[closest_index]) if route else None
        )
        task.safe_merge_point = dict(route[merge_index])
        task.remaining_route = list(route[merge_index:])
        configured = self._configured_takeover_route()
        if configured is not None:
            plan, handover_position, continuation = configured
            task.safe_merge_point = {
                "x": handover_position.x,
                "y": handover_position.y,
                "z": handover_position.z,
            }
            task.remaining_route = [
                {"x": point.x, "y": point.y, "z": point.z}
                for point in continuation
            ]
        task.status_reason = "hazard_route_progress_frozen"
        payload = {
            "vehicle_id": vehicle_id,
            "task_id": task.task_id,
            "closest_route_index": closest_index,
            "safe_merge_route_index": merge_index,
            "clearance_m": round(travelled, 3),
            "remaining_checkpoint_count": len(task.remaining_route),
            "safe_merge_point": dict(task.safe_merge_point),
            "takeover_strategy": (
                configured[0].get("strategy")
                if configured is not None
                else "dynamic_original_route_merge"
            ),
        }
        self._emit("hazard_task_route_frozen", payload)
        return payload

    def reassign_task(
        self,
        task_id: str,
        vehicle_id: str,
        speed_limit_kmh: Optional[float] = None,
        assignment_source: str = "manual_dispatch",
    ) -> Dict[str, object]:
        """Put a reassigned task first in a vehicle's existing task queue.

        ``manual_dispatch`` remains the backward-compatible default.  Scenario
        execution supplies a different source so evidence can distinguish a
        human-issued command from an approved scenario response.  In both
        cases an interrupted current task remains queued and resumes after
        the reassigned task reaches a terminal state.
        """

        self._require_connected()
        if vehicle_id not in self._actors:
            raise CarlaAdapterError(
                "Unknown CARLA vehicle: {}".format(vehicle_id)
            )
        if vehicle_id in self._faulted:
            raise CarlaAdapterError(
                "不能把任务分配给故障车辆：{}".format(vehicle_id)
            )
        if assignment_source not in {
            "manual_dispatch", "scenario_event", "scenario_recovery",
        }:
            raise CarlaAdapterError(
                "Unsupported reassignment source: {}".format(assignment_source)
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

        # The domain task is released before a human confirms takeover, so
        # assigned_vehicle_id is intentionally empty here. Detach the task
        # from its original hazard-stopped truck to avoid two vehicles
        # appearing to own the same work after reassignment.
        original_vehicle_id = task.original_vehicle_id
        if (
            original_vehicle_id
            and original_vehicle_id != vehicle_id
            and self._task_ids.get(original_vehicle_id) == task_id
        ):
            self._release_agent(original_vehicle_id)
            self._task_ids.pop(original_vehicle_id, None)
            self._task_queues[original_vehicle_id] = [
                queued_task_id
                for queued_task_id in self._task_queues.get(
                    original_vehicle_id, []
                )
                if queued_task_id != task_id
            ]
            self._task_status[original_vehicle_id] = (
                "emergency_stop"
                if original_vehicle_id in self._emergency_stopped
                else "idle"
            )

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

        # A reassignment has queue-front priority: the target vehicle's
        # current task returns to ``assigned`` but remains in its queue.
        current_target_task_id = self._task_ids.get(vehicle_id)
        if current_target_task_id and current_target_task_id != task_id:
            self._suspend_active_task(
                vehicle_id, current_target_task_id,
                reason="preempted_by_{}".format(assignment_source),
            )

        now = utc_now()
        task.assigned_vehicle_id = vehicle_id
        task.status = "assigned"
        task.updated_at = now
        task.started_at = None
        task.completed_at = None
        task.started_tick = None
        task.completed_tick = None
        task.status_reason = "assigned_by_{}".format(assignment_source)
        if speed_limit_kmh is not None:
            self._task_speed_limits[task_id] = float(speed_limit_kmh)

        takeover_route = self._build_takeover_route(task, vehicle_id)
        if takeover_route:
            self._takeover_routes[task_id] = takeover_route
            self._takeover_task_ids.add(task_id)
            task.transfer_count += 1
            task.status_reason = "takeover_route_ready_after_human_approval"

        mission_plan = self._mission_plans.get(task_id)
        if mission_plan:
            mission_plan["vehicle_id"] = vehicle_id
            # A frozen-route takeover continues the interrupted business
            # mission from the safe merge point.  Other reassignments first
            # travel to the task's service origin before executing it.
            mission_plan["phase"] = (
                "service_execution" if takeover_route
                else "pending_deadhead"
            )

        target_queue = self._task_queues.setdefault(vehicle_id, [])
        target_queue.insert(0, task_id)
        self._task_status[vehicle_id] = "assigned"
        self._start_next_task(vehicle_id)

        if old_vehicle_id and old_vehicle_id != vehicle_id:
            self._start_next_task(old_vehicle_id)

        self._emit(
            (
                "task_reassigned_by_human"
                if assignment_source == "manual_dispatch"
                else "task_reassigned_by_scenario"
            ),
            {
                "task_id": task_id,
                "old_vehicle_id": old_vehicle_id,
                "new_vehicle_id": vehicle_id,
                "assignment_source": assignment_source,
                "speed_limit_kmh": speed_limit_kmh,
                "takeover_route_checkpoint_count": len(takeover_route),
                "resumes_original_route": bool(takeover_route),
            },
        )
        return {
            "task_id": task_id,
            "old_vehicle_id": old_vehicle_id,
            "vehicle_id": vehicle_id,
            "status": self._task_status.get(vehicle_id, "assigned"),
            "assignment_source": assignment_source,
        }

    def _suspend_active_task(
        self, vehicle_id: str, task_id: str, reason: str,
    ) -> None:
        """Suspend one current task without leaking controller/hold state."""
        task = self._task_objects.get(task_id)
        if task is not None:
            task.status = "assigned"
            task.updated_at = utc_now()
            task.started_at = None
            task.started_tick = None
            task.status_reason = reason

        mission_plan = self._mission_plans.get(task_id)
        if mission_plan and mission_plan.get("phase") == "to_service_origin":
            # The resumed vehicle must explicitly restart the access leg; it
            # must not skip straight to the service target after preemption.
            mission_plan["phase"] = "pending_deadhead"

        hold = self._production_holds.pop(vehicle_id, None)
        production_plan = self._production_plans.get(task_id)
        if hold is not None and production_plan is not None:
            suspended_hold = dict(hold)
            suspended_hold["remaining_ticks"] = max(
                0, int(hold.get("until_tick", self._tick_index))
                - self._tick_index,
            )
            suspended_hold.pop("until_tick", None)
            suspended_hold["task_status"] = self._task_status.get(
                vehicle_id, str(production_plan.get("phase") or "loading")
            )
            production_plan["suspended_hold"] = suspended_hold

        self._release_agent(vehicle_id)
        self._task_ids.pop(vehicle_id, None)
        self._route_remaining_targets.pop(task_id, None)
        self._task_started_ticks.pop(task_id, None)
        self._task_status[vehicle_id] = "assigned"
        self._stop_vehicle(vehicle_id, hand_brake=True)
        self._emit("task_suspended_for_preemption", {
            "vehicle_id": vehicle_id,
            "task_id": task_id,
            "reason": reason,
            "production_hold_preserved": hold is not None,
        })

    def retarget_task(
        self, task_id: str, zone: ZoneConfig,
        speed_limit_kmh: Optional[float] = None,
    ) -> Dict[str, object]:
        """Move an active task to another admitted CARLA spawn target.

        This is the execution-side operation used by equipment/work-point
        scenarios.  The scenario layer decides *why* the target changes; the
        adapter only resolves and executes the new destination.
        """
        self._require_connected()
        task = self._task_objects.get(task_id)
        if task is None:
            raise CarlaAdapterError("Unknown task: {}".format(task_id))
        vehicle_id = task.assigned_vehicle_id
        if not vehicle_id or vehicle_id not in self._actors:
            raise CarlaAdapterError(
                "Task has no active CARLA vehicle: {}".format(task_id)
            )
        resolved = self.resolve_zones([zone])[0]
        self._zones_by_id[resolved.zone_id] = resolved
        task.zone_id = resolved.zone_id
        if speed_limit_kmh is not None:
            self._task_speed_limits[task_id] = float(speed_limit_kmh)
        self._release_agent(vehicle_id)
        self._task_ids.pop(vehicle_id, None)
        self._task_queues[vehicle_id] = [
            queued for queued in self._task_queues.get(vehicle_id, [])
            if queued != task_id
        ]
        task.status = "assigned"
        task.started_at = None
        task.started_tick = None
        task.status_reason = "retargeted_after_runtime_event"
        self._task_queues[vehicle_id].insert(0, task_id)
        self._start_next_task(vehicle_id)
        payload = {
            "task_id": task_id,
            "vehicle_id": vehicle_id,
            "zone_id": resolved.zone_id,
            "target_spawn_point_index": resolved.target_spawn_point_index,
            "speed_limit_kmh": speed_limit_kmh,
        }
        self._emit("task_retargeted_by_scenario", payload)
        return payload

    def set_task_route(
        self, task_id: str, vehicle_id: str,
        waypoint_positions: Sequence[Dict[str, float]],
        blocked_edge_id: Optional[str] = None,
        route_edge_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, object]:
        """Execute a selected RoadGraph route as successive BasicAgent legs."""
        self._require_connected()
        task = self._task_objects.get(task_id)
        actor = self._actors.get(vehicle_id)
        if task is None:
            raise CarlaAdapterError("Unknown task: {}".format(task_id))
        if actor is None:
            raise CarlaAdapterError("Unknown vehicle: {}".format(vehicle_id))
        if task.assigned_vehicle_id != vehicle_id:
            raise CarlaAdapterError(
                "Task {} is not assigned to {}".format(task_id, vehicle_id)
            )
        positions = [
            Position(float(item["x"]), float(item["y"]), float(item.get("z", 0.0)))
            for item in waypoint_positions
            if isinstance(item, dict) and "x" in item and "y" in item
        ]
        if not positions:
            raise CarlaAdapterError(
                "Task route requires at least one waypoint: {}".format(task_id)
            )
        location = actor.get_location()
        current = Position(location.x, location.y, location.z)
        nearest_index = min(
            range(len(positions)),
            key=lambda index: current.distance_to(positions[index]),
        )
        route = list(positions[nearest_index:])
        tolerance = self._task_arrival_tolerances.get(
            task_id, self.config.demo.arrival_tolerance_m
        )
        while len(route) > 1 and current.distance_to(route[0]) <= tolerance:
            route.pop(0)

        zone = self._zones_by_id.get(task.zone_id)
        if zone is not None:
            spawn_points = self.world.get_map().get_spawn_points()
            target_location = spawn_points[
                zone.target_spawn_point_index % len(spawn_points)
            ].location
            final_target = Position(
                target_location.x, target_location.y, target_location.z
            )
            if route[-1].distance_to(final_target) > 1.0:
                route.append(final_target)

        self._release_agent(vehicle_id)
        self._route_remaining_targets[task_id] = list(route[1:])
        self._hazard_replanned_task_ids.add(task_id)
        self._task_ids[vehicle_id] = task_id
        self._task_status[vehicle_id] = "executing"
        task.status = "executing"
        task.status_reason = "road_graph_route_applied"
        self._start_navigation_leg(vehicle_id, task_id, route[0])
        payload = {
            "task_id": task_id,
            "vehicle_id": vehicle_id,
            "waypoint_count": len(route),
            "route_edge_ids": list(route_edge_ids or []),
            "blocked_edge_id": blocked_edge_id,
            "status": "executing",
            "strategy": "road_graph_edges_via_basic_agent_legs",
        }
        self._emit("task_road_graph_route_applied", payload)
        return payload

    def set_task_speed_limit(
        self, task_id: str, speed_limit_kmh: float
    ) -> Dict[str, object]:
        """Apply a runtime speed limit and refresh the active BasicAgent."""
        self._require_connected()
        task = self._task_objects.get(task_id)
        if task is None:
            raise CarlaAdapterError("Unknown task: {}".format(task_id))
        value = max(1.0, float(speed_limit_kmh))
        self._task_speed_limits[task_id] = value
        vehicle_id = task.assigned_vehicle_id
        # Loading/dumping are stationary production service holds.  Persist
        # the speed cap now and let the next driving leg consume it; creating
        # a BasicAgent during the hold would leave two planners owning the
        # same CARLA actor when the hold expires.
        is_production_hold = bool(
            vehicle_id and vehicle_id in self._production_holds
        )
        if (
            vehicle_id
            and self._task_ids.get(vehicle_id) == task_id
            and not is_production_hold
        ):
            target = self._task_targets.get(task_id)
            self._release_agent(vehicle_id)
            if target is not None:
                self._start_navigation_leg(vehicle_id, task_id, target)
                self._task_status[vehicle_id] = "executing"
        payload = {
            "task_id": task_id,
            "vehicle_id": vehicle_id,
            "speed_limit_kmh": value,
        }
        self._emit("task_speed_limit_changed", payload)
        return payload

    def recover_task_navigation(
        self, task_id: str, forward_distance_m: float = 25.0
    ) -> Dict[str, object]:
        """Drive to a forward lane waypoint before retrying the current leg.

        A large mine truck can stop at a tight endpoint with an orientation
        from which rebuilding the same BasicAgent route is insufficient.  A
        short, lane-valid forward manoeuvre gives the controller room to
        replan without teleporting the actor or declaring the task complete.
        """
        self._require_connected()
        task = self._task_objects.get(task_id)
        vehicle_id = task.assigned_vehicle_id if task is not None else None
        if (
            task is None or not vehicle_id
            or self._task_ids.get(vehicle_id) != task_id
            or vehicle_id in self._faulted
            or vehicle_id in self._paused
            or vehicle_id in self._emergency_stopped
        ):
            return {
                "task_id": task_id,
                "vehicle_id": vehicle_id,
                "status": "NOT_AVAILABLE",
                "reason": "task_is_not_actively_navigating",
            }
        actor = self._actors.get(vehicle_id)
        current_target = self._task_targets.get(task_id)
        if actor is None or current_target is None:
            return {
                "task_id": task_id,
                "vehicle_id": vehicle_id,
                "status": "NOT_AVAILABLE",
                "reason": "actor_or_target_missing",
            }
        try:
            carla_map = self.world.get_map()
            lane_type = getattr(
                getattr(self.carla, "LaneType", None), "Driving", None
            )
            kwargs = {"project_to_road": True}
            if lane_type is not None:
                kwargs["lane_type"] = lane_type
            waypoint = carla_map.get_waypoint(actor.get_location(), **kwargs)
            candidates = list(waypoint.next(max(10.0, float(forward_distance_m)))) \
                if waypoint is not None else []
        except (AttributeError, RuntimeError, TypeError, ValueError):
            candidates = []
        if not candidates:
            return {
                "task_id": task_id,
                "vehicle_id": vehicle_id,
                "status": "NOT_AVAILABLE",
                "reason": "no_forward_driving_waypoint",
            }
        actor_location = actor.get_location()
        candidate = max(
            candidates,
            key=lambda item: math.hypot(
                item.transform.location.x - actor_location.x,
                item.transform.location.y - actor_location.y,
            ),
        )
        location = candidate.transform.location
        for other_vehicle_id, other_actor in self._actors.items():
            if other_vehicle_id == vehicle_id:
                continue
            try:
                other = other_actor.get_location()
            except (AttributeError, RuntimeError):
                continue
            if math.sqrt(
                (location.x - other.x) ** 2
                + (location.y - other.y) ** 2
                + (location.z - other.z) ** 2
            ) < 20.0:
                return {
                    "task_id": task_id,
                    "vehicle_id": vehicle_id,
                    "status": "NOT_AVAILABLE",
                    "reason": "forward_waypoint_occupied",
                }

        retry_targets = [current_target] + list(
            self._route_remaining_targets.get(task_id, [])
        )
        recovery_target = Position(location.x, location.y, location.z)
        self._route_remaining_targets[task_id] = retry_targets
        self._release_agent(vehicle_id)
        self._start_navigation_leg(vehicle_id, task_id, recovery_target)
        task.status = "executing"
        task.status_reason = "navigation_recovery_waypoint_started"
        self._task_status[vehicle_id] = "executing"
        payload = {
            "task_id": task_id,
            "vehicle_id": vehicle_id,
            "status": "APPLIED",
            "strategy": "FORWARD_LANE_WAYPOINT_THEN_RETRY_CURRENT_LEG",
            "forward_distance_m": float(forward_distance_m),
            "recovery_target": {
                "x": recovery_target.x,
                "y": recovery_target.y,
                "z": recovery_target.z,
            },
        }
        self._emit("task_navigation_recovery_started", payload)
        return payload

    def set_task_arrival_tolerance(
        self, task_id: str, arrival_tolerance_m: float
    ) -> Dict[str, object]:
        """Apply the endpoint tolerance recorded by physical route evidence."""
        self._require_connected()
        if task_id not in self._task_objects:
            raise CarlaAdapterError("Unknown task: {}".format(task_id))
        value = max(0.5, float(arrival_tolerance_m))
        self._task_arrival_tolerances[task_id] = value
        payload = {
            "task_id": task_id,
            "arrival_tolerance_m": value,
            "source": "P6_PHYSICAL_ROUTE_VALIDATION",
        }
        self._emit("task_arrival_tolerance_changed", payload)
        return payload

    def _build_takeover_route(
        self, task: Task, vehicle_id: str
    ) -> List[Position]:
        """Build access checkpoints, then append the frozen original route."""

        actor = self._actors.get(vehicle_id)
        configured = self._configured_takeover_route(vehicle_id)
        if actor is not None and configured is not None:
            plan, handover_position, continuation = configured
            task.safe_merge_point = {
                "x": handover_position.x,
                "y": handover_position.y,
                "z": handover_position.z,
            }
            task.remaining_route = [
                {"x": point.x, "y": point.y, "z": point.z}
                for point in continuation
            ]
            self._emit(
                "hazard_task_takeover_route_built",
                {
                    "task_id": task.task_id,
                    "original_vehicle_id": task.original_vehicle_id,
                    "takeover_vehicle_id": vehicle_id,
                    "access_checkpoint_count": 0,
                    "total_checkpoint_count": len(continuation),
                    "safe_merge_point": dict(task.safe_merge_point),
                    "strategy": plan.get("strategy"),
                    "blocked_segment_is_skipped": bool(
                        plan.get("blocked_segment_is_skipped", False)
                    ),
                },
            )
            return list(continuation)
        merge = task.safe_merge_point
        if actor is None or not merge or not task.remaining_route:
            return []

        merge_position = Position(
            float(merge["x"]),
            float(merge["y"]),
            float(merge.get("z", 0.0)),
        )
        access_route = self._trace_route_positions(
            actor.get_location(), merge_position, sampling_resolution=4.0
        )
        if not access_route:
            access_route = [merge_position]
        original_remaining = [
            Position(
                float(point["x"]),
                float(point["y"]),
                float(point.get("z", 0.0)),
            )
            for point in task.remaining_route
        ]
        checkpoints = self._sample_route_positions(
            access_route, spacing_m=18.0
        )
        checkpoints.extend(
            self._sample_route_positions(
                original_remaining, spacing_m=18.0
            )
        )

        deduplicated: List[Position] = []
        current = actor.get_location()
        current_position = Position(current.x, current.y, current.z)
        for point in checkpoints:
            if point.distance_to(current_position) < 8.0:
                continue
            if deduplicated and point.distance_to(deduplicated[-1]) < 4.0:
                continue
            deduplicated.append(point)
        self._emit(
            "hazard_task_takeover_route_built",
            {
                "task_id": task.task_id,
                "original_vehicle_id": task.original_vehicle_id,
                "takeover_vehicle_id": vehicle_id,
                "access_checkpoint_count": len(
                    self._sample_route_positions(
                        access_route, spacing_m=18.0
                    )
                ),
                "total_checkpoint_count": len(deduplicated),
                "safe_merge_point": dict(merge),
                "planner_sampling_resolution_m": 4.0,
            },
        )
        return deduplicated

    def _configured_takeover_route(
        self, vehicle_id: Optional[str] = None
    ) -> Optional[Tuple[Dict[str, object], Position, List[Position]]]:
        slope_event = self.config.scenario_variables.get("slope_event", {})
        if not isinstance(slope_event, dict):
            return None
        plan = slope_event.get("takeover_plan", {})
        if not isinstance(plan, dict):
            return None
        configured_vehicle_id = str(
            plan.get("takeover_vehicle_id", "")
        ).strip()
        eligible_vehicle_ids = plan.get("eligible_vehicle_ids", [])
        if isinstance(eligible_vehicle_ids, list) and eligible_vehicle_ids:
            eligible_vehicle_ids = {
                str(item) for item in eligible_vehicle_ids
            }
            if (
                vehicle_id is not None
                and vehicle_id not in eligible_vehicle_ids
            ):
                return None
        elif (
            vehicle_id is not None
            and configured_vehicle_id
            and configured_vehicle_id != vehicle_id
        ):
            return None
        handover_index = plan.get("handover_spawn_point_index")
        waypoint_indices = plan.get(
            "continuation_waypoint_spawn_point_indices", []
        )
        if (
            self.world is None
            or not isinstance(waypoint_indices, list)
            or not waypoint_indices
        ):
            return None
        spawn_points = self.world.get_map().get_spawn_points()
        if not spawn_points:
            return None

        def position_for(index: int) -> Position:
            location = spawn_points[int(index) % len(spawn_points)].location
            return Position(location.x, location.y, location.z)

        continuation = [
            position_for(int(index)) for index in waypoint_indices
        ]
        actor = self._actors.get(vehicle_id) if vehicle_id else None
        if actor is not None:
            location = actor.get_location()
            handover_position = Position(
                location.x, location.y, location.z
            )
        elif handover_index is not None:
            handover_position = position_for(int(handover_index))
        else:
            handover_position = continuation[0]
        return plan, handover_position, continuation

    def _trace_route_positions(
        self, start_location, target: Position, sampling_resolution: float
    ) -> List[Position]:
        if (
            self.world is None
            or self.carla is None
            or self._global_route_planner_class is None
            or self._global_route_planner_dao_class is None
        ):
            return []
        try:
            dao = self._global_route_planner_dao_class(
                self.world.get_map(), float(sampling_resolution)
            )
            planner = self._global_route_planner_class(dao)
            planner.setup()
            route = planner.trace_route(
                start_location,
                self.carla.Location(
                    x=target.x, y=target.y, z=target.z
                ),
            )
        except Exception as exc:
            self._emit(
                "takeover_access_route_fallback",
                {"reason": str(exc), "strategy": "direct_safe_merge"},
            )
            return []
        positions = []
        for item in route:
            waypoint = item[0] if isinstance(item, (tuple, list)) else item
            location = waypoint.transform.location
            positions.append(Position(location.x, location.y, location.z))
        return positions

    @staticmethod
    def _sample_route_positions(
        route: Sequence[Position], spacing_m: float
    ) -> List[Position]:
        if not route:
            return []
        sampled = [route[0]]
        travelled = 0.0
        previous = route[0]
        for point in route[1:]:
            travelled += point.distance_to(previous)
            previous = point
            if travelled >= spacing_m:
                sampled.append(point)
                travelled = 0.0
        if sampled[-1].distance_to(route[-1]) > 1.0:
            sampled.append(route[-1])
        return sampled

    def close(self) -> None:
        self._destroy_camera_streams()
        for vehicle_id in list(self._agents):
            self._stop_vehicle(
                vehicle_id, hand_brake=True
            )
            self._release_agent(vehicle_id)
        self._task_queues.clear()
        self._task_objects.clear()
        self._zones_by_id.clear()
        self._task_targets.clear()
        self._route_remaining_targets.clear()
        self._production_plans.clear()
        self._production_holds.clear()
        self._mission_plans.clear()
        self._takeover_routes.clear()
        self._task_speed_limits.clear()
        self._task_arrival_tolerances.clear()
        self._takeover_task_ids.clear()
        self._task_started_ticks.clear()
        self._spectator = None
        self._camera_vehicle_id = None
        self._camera_frame_numbers.clear()
        self._camera_display_names.clear()
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

    def destroy_spawned_vehicles(self) -> int:
        """Destroy only vehicle actors created by this adapter instance."""

        # Attached camera sensors must be stopped before their parent vehicle.
        self._destroy_camera_streams()
        destroyed = 0
        spawned_ids = set(self.spawned_actor_ids)
        for vehicle_id, actor in list(self._actors.items()):
            if getattr(actor, "id", None) not in spawned_ids:
                continue
            try:
                actor.destroy()
                destroyed += 1
            except (AttributeError, RuntimeError):
                pass
            self._actors.pop(vehicle_id, None)
        return destroyed

    def retire_vehicle(
        self, vehicle_id: str,
        reason: str = "runtime_route_clearance",
    ) -> Dict[str, object]:
        """Remove an owned idle/stuck actor while retaining factual evidence.

        The current mine map has no validated parking resources.  Leaving a
        completed full-size truck on an active road is less truthful and less
        safe than ending that actor's participation in the finite episode.
        Externally owned actors are never destroyed by this adapter.
        """
        actor = self._actors.get(vehicle_id)
        if actor is None:
            return {
                "vehicle_id": vehicle_id,
                "status": "ALREADY_ABSENT",
                "reason": reason,
            }
        actor_id = getattr(actor, "id", None)
        if actor_id not in set(self.spawned_actor_ids):
            self._stop_vehicle(vehicle_id, hand_brake=True)
            return {
                "vehicle_id": vehicle_id,
                "actor_id": actor_id,
                "status": "NOT_OWNED_SAFE_HOLD",
                "reason": reason,
            }
        location = actor.get_location()
        self._release_agent(vehicle_id)
        sensor = self._camera_sensors.pop(vehicle_id, None)
        self._camera_display_names.pop(vehicle_id, None)
        self._camera_frame_numbers.pop(vehicle_id, None)
        if sensor is not None:
            try:
                sensor.stop()
            except (AttributeError, RuntimeError):
                pass
            try:
                sensor.destroy()
            except (AttributeError, RuntimeError):
                pass
        try:
            actor.destroy()
        except (AttributeError, RuntimeError) as exc:
            return {
                "vehicle_id": vehicle_id,
                "actor_id": actor_id,
                "status": "DESTROY_FAILED_SAFE_HOLD",
                "reason": reason,
                "error": str(exc),
            }
        self._actors.pop(vehicle_id, None)
        self._task_ids.pop(vehicle_id, None)
        self._task_queues[vehicle_id] = []
        self._production_holds.pop(vehicle_id, None)
        self._paused.discard(vehicle_id)
        self._emergency_stopped.discard(vehicle_id)
        self._task_status[vehicle_id] = (
            "retired_after_completion"
            if reason == "completed_vehicle_clears_active_routes"
            else "retired_after_execution_failure"
        )
        payload = {
            "vehicle_id": vehicle_id,
            "actor_id": actor_id,
            "status": "RETIRED_FROM_EPISODE",
            "reason": reason,
            "last_position": {
                "x": float(location.x),
                "y": float(location.y),
                "z": float(location.z),
            },
            "parking_resource_status": "NOT_AVAILABLE",
            "task_record_retained": True,
        }
        self._emit("vehicle_retired_from_episode", payload)
        return payload

    def _camera_wall_options(self) -> Dict[str, object]:
        options = self.config.scenario_variables.get("camera_wall", {})
        return options if isinstance(options, dict) else {}

    def _ensure_camera_streams(self) -> None:
        """Create one overview camera and one chase camera per mine truck.

        The simulator and API run in separate processes, so callbacks publish
        only the latest PNG for each stream to an atomic shared-file handoff.
        Camera failures remain presentation-only and never stop dispatching.
        """

        options = self._camera_wall_options()
        if not bool(options.get("enabled", False)):
            return
        if self._camera_sensors or self.world is None or self.carla is None:
            return
        if not self._actors:
            return

        try:
            self._camera_output_dir.mkdir(parents=True, exist_ok=True)
            blueprint = self.world.get_blueprint_library().find(
                "sensor.camera.rgb"
            )
            width = max(320, int(options.get("image_width", 640)))
            height = max(180, int(options.get("image_height", 360)))
            sensor_tick = max(
                0.1, float(options.get("sensor_tick_seconds", 0.25))
            )
            blueprint.set_attribute("image_size_x", str(width))
            blueprint.set_attribute("image_size_y", str(height))
            blueprint.set_attribute("fov", str(options.get("fov", 90)))
            blueprint.set_attribute("sensor_tick", str(sensor_tick))

            overview = self.world.spawn_actor(
                blueprint,
                self._overview_camera_transform(options),
            )
            self._register_camera_sensor(
                "global", "矿山全局视角", overview
            )

            definitions = {
                item.vehicle_id: item for item in self.config.vehicles
            }
            requested_ids = options.get("vehicle_ids")
            if not isinstance(requested_ids, list):
                requested_ids = sorted(self._actors)
            for vehicle_id in requested_ids:
                vehicle_id = str(vehicle_id)
                actor = self._actors.get(vehicle_id)
                if actor is None:
                    continue
                sensor = self.world.spawn_actor(
                    blueprint,
                    self._chase_camera_transform(actor, options),
                    attach_to=actor,
                )
                definition = definitions.get(vehicle_id)
                display_name = (
                    definition.display_name if definition else vehicle_id
                )
                self._register_camera_sensor(
                    vehicle_id, display_name, sensor
                )
            self._write_camera_manifest(
                width=width,
                height=height,
                sensor_tick=sensor_tick,
            )
            self._emit(
                "camera_wall_started",
                {
                    "stream_ids": sorted(self._camera_sensors),
                    "image_width": width,
                    "image_height": height,
                    "sensor_tick_seconds": sensor_tick,
                },
            )
        except (AttributeError, RuntimeError, OSError, TypeError, ValueError) as exc:
            self._destroy_camera_streams()
            print(
                "WARNING: 多车视频监控启动失败，不影响车辆调度：{}".format(
                    exc
                ), file=sys.stderr
            )

    def _overview_camera_transform(self, options: Dict[str, object]):
        spawn_points = self.world.get_map().get_spawn_points()
        locations = [item.location for item in spawn_points]
        if locations:
            min_x = min(item.x for item in locations)
            max_x = max(item.x for item in locations)
            min_y = min(item.y for item in locations)
            max_y = max(item.y for item in locations)
            max_z = max(item.z for item in locations)
            center_x = (min_x + max_x) / 2.0
            center_y = (min_y + max_y) / 2.0
            span = max(max_x - min_x, max_y - min_y)
        else:
            center_x = center_y = max_z = 0.0
            span = 300.0
        camera_height = float(
            options.get(
                "overview_height_m",
                max(160.0, min(600.0, span * 0.65)),
            )
        )
        return self.carla.Transform(
            self.carla.Location(
                x=center_x,
                y=center_y,
                z=max_z + camera_height,
            ),
            self.carla.Rotation(pitch=-90.0, yaw=0.0, roll=0.0),
        )

    def _chase_camera_transform(
        self, actor, options: Dict[str, object]
    ):
        bounding_box = getattr(actor, "bounding_box", None)
        extent = getattr(bounding_box, "extent", None)
        half_length = float(getattr(extent, "x", 0.0))
        half_height = float(getattr(extent, "z", 0.0))
        follow_distance = float(
            options.get(
                "follow_distance_m",
                max(20.0, half_length * 2.0 + 8.0),
            )
        )
        camera_height = float(
            options.get(
                "follow_height_m",
                max(10.0, half_height + 7.0),
            )
        )
        return self.carla.Transform(
            self.carla.Location(
                x=-follow_distance,
                y=0.0,
                z=camera_height,
            ),
            self.carla.Rotation(pitch=-16.0, yaw=0.0, roll=0.0),
        )

    def _register_camera_sensor(
        self, stream_id: str, display_name: str, sensor
    ) -> None:
        self._camera_sensors[stream_id] = sensor
        self._camera_display_names[stream_id] = display_name
        self.spawned_actor_ids.append(sensor.id)
        sensor.listen(
            lambda image, camera_id=stream_id: self._save_camera_frame(
                camera_id, image
            )
        )

    def _save_camera_frame(self, stream_id: str, image) -> None:
        if stream_id not in self._camera_sensors:
            return
        frame_number = int(getattr(image, "frame", 0))
        if self._camera_frame_numbers.get(stream_id) == frame_number:
            return
        staging_path = self._camera_output_dir / (
            "{}.next.png".format(stream_id)
        )
        target_path = self._camera_output_dir / (
            "{}.png".format(stream_id)
        )
        try:
            image.save_to_disk(str(staging_path))
            os.replace(str(staging_path), str(target_path))
            self._camera_frame_numbers[stream_id] = frame_number
        except (RuntimeError, OSError):
            return

    def _write_camera_manifest(
        self, width: int, height: int, sensor_tick: float
    ) -> None:
        streams = []
        for stream_id, sensor in self._camera_sensors.items():
            streams.append(
                {
                    "id": stream_id,
                    "name": getattr(
                        sensor,
                        "_openpit_display_name",
                        self._camera_display_names.get(stream_id, stream_id),
                    ),
                    "kind": (
                        "overview" if stream_id == "global" else "vehicle"
                    ),
                    "image_url": "/camera/{}/frame".format(stream_id),
                }
            )
        payload = {
            "status": "online",
            "width": width,
            "height": height,
            "sensor_tick_seconds": sensor_tick,
            "streams": streams,
        }
        staging_path = self._camera_output_dir / "manifest.next.json"
        target_path = self._camera_output_dir / "manifest.json"
        staging_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(staging_path), str(target_path))

    def _destroy_camera_streams(self) -> None:
        sensors = list(self._camera_sensors.values())
        self._camera_sensors.clear()
        self._camera_display_names.clear()
        for sensor in sensors:
            try:
                sensor.stop()
            except (AttributeError, RuntimeError):
                pass
            try:
                sensor.destroy()
            except (AttributeError, RuntimeError):
                pass
        manifest_path = self._camera_output_dir / "manifest.json"
        try:
            if manifest_path.exists():
                manifest_path.unlink()
        except OSError:
            pass

    def drain_events(self) -> List[Dict[str, object]]:
        events = list(self._events)
        self._events.clear()
        return events

    def _import_carla(self) -> None:
        # A scenario config can originate on another validation computer.
        # Prefer the explicit local override while preserving the configured
        # path as the backward-compatible fallback.
        configured_root = os.environ.get("OPENPIT_CARLA_ROOT") or self.config.carla.root
        root = Path(configured_root).expanduser().resolve()
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
            from agents.navigation.global_route_planner import (
                GlobalRoutePlanner,
            )
            from agents.navigation.global_route_planner_dao import (
                GlobalRoutePlannerDAO,
            )
        except ImportError as exc:
            raise CarlaAdapterError(
                "Failed to import CARLA API/BasicAgent: {}".format(exc)
            ) from exc
        self.carla = carla
        self._basic_agent_class = BasicAgent
        self._global_route_planner_class = GlobalRoutePlanner
        self._global_route_planner_dao_class = GlobalRoutePlannerDAO

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
            production_plan = self._production_plans.get(task.task_id)
            mission_plan = self._mission_plans.get(task.task_id)
            suspended_hold = (
                production_plan.pop("suspended_hold", None)
                if production_plan else None
            )
            if suspended_hold is not None:
                now = utc_now()
                task.status = "executing"
                task.updated_at = now
                task.started_at = now
                task.started_tick = self._tick_index
                task.completed_at = None
                task.completed_tick = None
                task.attempt_count += 1
                task.status_reason = "resumed_preempted_production_hold"
                restored_hold = dict(suspended_hold)
                restored_hold["until_tick"] = self._tick_index + int(
                    restored_hold.pop("remaining_ticks", 0)
                )
                restored_status = str(
                    restored_hold.pop("task_status", None)
                    or production_plan.get("phase")
                    or "production_service_hold"
                )
                self._production_holds[vehicle_id] = restored_hold
                self._task_ids[vehicle_id] = task.task_id
                self._task_status[vehicle_id] = restored_status
                self._task_started_ticks[task.task_id] = self._tick_index
                self._stop_vehicle(vehicle_id, hand_brake=True)
                self._emit("task_production_hold_resumed", {
                    "vehicle_id": vehicle_id,
                    "task_id": task.task_id,
                    "status": restored_status,
                    "remaining_ticks": max(
                        0, int(restored_hold["until_tick"]) - self._tick_index
                    ),
                })
                return
            if mission_plan and mission_plan.get("phase") == "pending_deadhead":
                now = utc_now()
                service_origin = mission_plan["service_origin"]
                task.status = "executing"
                task.updated_at = now
                task.started_at = now
                task.started_tick = self._tick_index
                task.completed_at = None
                task.completed_tick = None
                task.attempt_count += 1
                task.status_reason = "deadhead_to_service_origin"
                task.last_distance_m = round(
                    Position(
                        actor.get_location().x,
                        actor.get_location().y,
                        actor.get_location().z,
                    ).distance_to(service_origin),
                    3,
                )
                mission_plan["phase"] = "to_service_origin"
                self._task_targets[task.task_id] = service_origin
                self._task_started_ticks[task.task_id] = self._tick_index
                self._task_ids[vehicle_id] = task.task_id
                self._task_status[vehicle_id] = "deadhead_to_service_origin"
                self._start_navigation_leg(
                    vehicle_id, task.task_id, service_origin
                )
                self._emit("task_mission_stage_changed", {
                    "vehicle_id": vehicle_id,
                    "task_id": task.task_id,
                    "from_status": "spawned",
                    "to_status": "to_service_origin",
                    "reason": "admitted_deadhead_route_started",
                    "service_origin_point_id": mission_plan.get(
                        "service_origin_point_id"
                    ),
                    "deadhead_route_evidence": mission_plan.get(
                        "deadhead_route_evidence"
                    ),
                })
                return
            if production_plan and production_plan.get("phase") == "pending_loading":
                self._begin_loading_hold(
                    vehicle_id, task, production_plan, target
                )
                return
            self._parked.discard(vehicle_id)
            actor.apply_control(
                self.carla.VehicleControl(
                    throttle=0.0,
                    brake=0.0,
                    hand_brake=False,
                )
            )
            navigation_target = target
            safe_route_applied = False
            takeover_route_applied = False
            safe_route_plan = self._safe_route_plan or {}
            safe_task_types = safe_route_plan.get("task_types", set())
            takeover_route = self._takeover_routes.get(task.task_id, [])
            if takeover_route:
                navigation_target = takeover_route[0]
                self._route_remaining_targets[task.task_id] = list(
                    takeover_route[1:]
                )
                takeover_route_applied = True
            elif task.task_type in safe_task_types:
                waypoints = safe_route_plan.get("waypoint_positions", [])
                if waypoints and all(
                    isinstance(item, Position) for item in waypoints
                ):
                    navigation_target = waypoints[0]
                    self._route_remaining_targets[task.task_id] = list(
                        waypoints[1:]
                    ) + [target]
                    self._hazard_replanned_task_ids.add(task.task_id)
                    safe_route_applied = True
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
            task.status_reason = (
                "takeover_navigation_started"
                if takeover_route_applied
                else "navigation_started"
            )
            if mission_plan and mission_plan.get("phase") == "at_service_origin":
                mission_plan["phase"] = "service_execution"
            self._task_targets[task.task_id] = navigation_target
            self._task_started_ticks[task.task_id] = self._tick_index
            self._task_ids[vehicle_id] = task.task_id
            self._task_status[vehicle_id] = "executing"
            self._start_navigation_leg(
                vehicle_id, task.task_id, navigation_target
            )
            if not task.original_route and not takeover_route_applied:
                task.original_route = self._extract_route_points(vehicle_id)
            self._emit(
                "task_started",
                {
                    "vehicle_id": vehicle_id,
                    "task_id": task.task_id,
                    "attempt_count": task.attempt_count,
                    "initial_distance_m": task.last_distance_m,
                    "target_spawn_point_index": target_index,
                    "target_position": {
                        "x": navigation_target.x,
                        "y": navigation_target.y,
                        "z": navigation_target.z,
                    },
                    "final_target_position": {
                        "x": target.x,
                        "y": target.y,
                        "z": target.z,
                    },
                    "safe_route_applied": safe_route_applied,
                    "takeover_route_applied": takeover_route_applied,
                    "takeover_route_checkpoint_count": len(takeover_route),
                    "safe_route_plan_id": safe_route_plan.get(
                        "route_plan_id"
                    ) if safe_route_applied else None,
                },
            )
            return
        self._task_status[vehicle_id] = "idle"
        self._park_vehicle_off_route(vehicle_id)
        self._stop_vehicle(vehicle_id, hand_brake=True)

    def _start_navigation_leg(
        self, vehicle_id: str, task_id: str, target: Position
    ) -> None:
        actor = self._actors[vehicle_id]
        # A vehicle can have only one controller owner.  CARLA 0.9.10's
        # LocalPlanner destructor destroys its ego vehicle, so replacing an
        # existing BasicAgent without detaching it first also destroys the
        # shared mine-truck actor.
        if vehicle_id in self._agents:
            self._release_agent(vehicle_id)
        definition = next(
            item
            for item in self.config.vehicles
            if item.vehicle_id == vehicle_id
        )
        target_speed = self._task_speed_limits.get(
            task_id, definition.target_speed_kmh
        )
        agent = None
        try:
            agent = self._basic_agent_class(actor, target_speed=target_speed)
            # CARLA 0.9.10 LocalPlanner.__del__ destroys its ego vehicle.
            # Register before route construction so every failure path can
            # safely detach that planner instead of deleting a mine truck.
            self._agents[vehicle_id] = agent
            agent.set_destination([target.x, target.y, target.z])
            local_planner = self._local_planner_for(agent)
            speed_setter = getattr(local_planner, "set_speed", None)
            if callable(speed_setter):
                speed_setter(float(target_speed))
            self._task_targets[task_id] = target
        except Exception as exc:
            self._release_agent(vehicle_id)
            raise CarlaAdapterError(
                "Failed to initialize CARLA route for task {} on {}: {}"
                .format(task_id, vehicle_id, exc)
            ) from exc

    @staticmethod
    def _local_planner_for(agent):
        getter = getattr(agent, "get_local_planner", None)
        if callable(getter):
            try:
                planner = getter()
                if planner is not None:
                    return planner
            except Exception:
                pass
        return getattr(agent, "_local_planner", None)

    def _park_vehicle_off_route(self, vehicle_id: str) -> None:
        offset = self.config.demo.idle_pull_over_offset_m
        actor = self._actors.get(vehicle_id)
        if (
            offset <= 0
            or actor is None
            or vehicle_id in self._parked
            or vehicle_id not in self._vehicles_with_completed_task
            or self._all_tasks_terminal()
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

    def _all_tasks_terminal(self) -> bool:
        """Return whether parking can no longer unblock another task."""

        terminal_statuses = {
            "completed",
            "timed_out",
            "cancelled",
        }
        return bool(self._task_objects) and all(
            task.status in terminal_statuses
            for task in self._task_objects.values()
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
        if hand_brake:
            zero_velocity = self.carla.Vector3D(
                x=0.0,
                y=0.0,
                z=0.0,
            )
            actor.set_target_velocity(zero_velocity)
            actor.set_target_angular_velocity(zero_velocity)

    def _update_spectator_camera(self) -> None:
        if self._spectator is None or self.carla is None:
            return
        camera_director = self.config.scenario_variables.get(
            "camera_director", {}
        )
        preferred_vehicle_id = (
            str(camera_director.get("follow_vehicle_id", "")).strip()
            if isinstance(camera_director, dict)
            and camera_director.get("mode") == "fixed_vehicle_chase"
            else ""
        )
        preferred_actor = self._actors.get(preferred_vehicle_id)
        if preferred_actor is not None:
            vehicle_id = preferred_vehicle_id
            actor = preferred_actor
            task_id = self._task_ids.get(vehicle_id)
            task = self._task_objects.get(task_id) if task_id else None
            camera_mode = "fixed_vehicle_chase"
        else:
            vehicle_id = None
            actor = None
            task_id = None
            task = None
            camera_mode = "priority_vehicle_chase"
        candidates = []
        if actor is None:
            for candidate_vehicle_id, candidate_task_id in self._task_ids.items():
                candidate_task = self._task_objects.get(candidate_task_id)
                candidate_actor = self._actors.get(candidate_vehicle_id)
                if candidate_task is None or candidate_actor is None:
                    continue
                candidates.append(
                    (
                        candidate_task.priority,
                        candidate_vehicle_id,
                        candidate_actor,
                        candidate_task,
                    )
                )
            if not candidates:
                return
            _, vehicle_id, actor, task = max(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            task_id = task.task_id
        actor_transform = actor.get_transform()
        forward = actor_transform.get_forward_vector()
        actor_location = actor_transform.location
        bounding_box = getattr(actor, "bounding_box", None)
        extent = getattr(bounding_box, "extent", None)
        # Mine-truck bodies are substantially larger than CARLA passenger
        # cars.  Derive a camera offset from the real actor bounds so the
        # spectator stays outside the chassis rather than under the axle.
        body_half_length = float(getattr(extent, "x", 0.0))
        body_half_height = float(getattr(extent, "z", 0.0))
        follow_distance = max(20.0, body_half_length * 2.0 + 8.0)
        camera_height = max(10.0, body_half_height + 7.0)
        camera_location = self.carla.Location(
            x=actor_location.x - forward.x * follow_distance,
            y=actor_location.y - forward.y * follow_distance,
            z=actor_location.z + camera_height,
        )
        actor_rotation = actor_transform.rotation
        camera_rotation = self.carla.Rotation(
            pitch=-16.0,
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
                    "task_id": task_id,
                    "camera_mode": camera_mode,
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
        local_planner = self._local_planner_for(agent)
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
