"""
OpenPit Agent运行时状态中心。

run_demo.py通过HTTP /runtime/sync向API进程推送状态。
本版本在动态车辆点位基础上，增加CARLA道路、任务区域、
规划路线和历史轨迹的地图数据管理。
"""

from datetime import datetime
from threading import RLock
from typing import Any, Dict, List, Optional
from uuid import uuid4


class RuntimeState:

    def __init__(self):
        self.vehicles: List[Dict[str, Any]] = []
        self.tasks: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []

        self.agents = [
            {
                "name": "Risk Agent",
                "status": "ONLINE",
                "function": "风险评估",
            },
            {
                "name": "Scheduler Agent",
                "status": "ONLINE",
                "function": "任务动态调度",
            },
            {
                "name": "Memory Agent",
                "status": "ONLINE",
                "function": "历史经验学习",
            },
        ]

        self.environment: Dict[str, Any] = {}
        self.risk: Dict[str, Any] = {
            "level": "UNKNOWN",
            "assessment": [],
        }
        self.decision: Dict[str, Any] = {
            "status": "PASS",
        }
        self.monitoring: Dict[str, Any] = {
            "phase": "等待运行",
            "phase_index": 0,
            "fixed_station_count": 0,
            "mobile_equipment_count": 0,
            "fixed_observation_count": 0,
            "mobile_observation_count": 0,
            "risk_level": "UNKNOWN",
            "previous_risk_level": "UNKNOWN",
            "work_order_count": 0,
            "closed_work_order_count": 0,
            "feedback_count": 0,
            "road_control_status": "未启动",
            "route_safety_status": "等待路线规划",
            "route_avoidance_enforced": False,
            "closed_loop_complete": False,
            "updated_at": None,
        }
        self.run_id = "runtime"
        self.map_name = "Town03"

        # UI写入、CARLA运行进程读取的跨进程命令队列。
        self.commands: List[Dict[str, Any]] = []
        self._command_lock = RLock()

    def reset(self):
        self.vehicles = []
        self.tasks = []
        self.events = []
        self.environment = {}
        self.risk = {
            "level": "UNKNOWN",
            "assessment": [],
        }
        self.decision = {
            "status": "PASS",
        }
        self.monitoring = {
            "phase": "等待运行",
            "phase_index": 0,
            "fixed_station_count": 0,
            "mobile_equipment_count": 0,
            "fixed_observation_count": 0,
            "mobile_observation_count": 0,
            "risk_level": "UNKNOWN",
            "previous_risk_level": "UNKNOWN",
            "work_order_count": 0,
            "closed_work_order_count": 0,
            "feedback_count": 0,
            "road_control_status": "未启动",
            "route_safety_status": "等待路线规划",
            "route_avoidance_enforced": False,
            "closed_loop_complete": False,
            "updated_at": None,
        }
        self.run_id = "runtime"
        self.map_name = "Town03"
        with self._command_lock:
            self.commands = []

    def add_event(self, event_type, message):
        self.events.append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": event_type,
                "message": message,
            }
        )
        if len(self.events) > 1000:
            self.events = self.events[-1000:]

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @classmethod
    def _normalize_position(cls, position) -> Optional[Dict[str, float]]:
        if not isinstance(position, dict):
            return None
        return {
            "x": cls._to_float(position.get("x", 0.0)),
            "y": cls._to_float(position.get("y", 0.0)),
            "z": cls._to_float(position.get("z", 0.0)),
        }

    @classmethod
    def _normalize_points(cls, points) -> List[Dict[str, float]]:
        normalized = []
        if not isinstance(points, list):
            return normalized
        for point in points:
            item = cls._normalize_position(point)
            if item is not None:
                normalized.append(item)
        return normalized

    @classmethod
    def _format_position(cls, position):
        normalized = cls._normalize_position(position)
        if normalized is None:
            if isinstance(position, str):
                return position
            return "-"
        return "({:.1f}, {:.1f}, {:.1f})".format(
            normalized["x"],
            normalized["y"],
            normalized["z"],
        )

    @classmethod
    def _normalize_vehicle(cls, vehicle):
        vehicle_id = (
            vehicle.get("id")
            or vehicle.get("vehicle_id")
            or "unknown_vehicle"
        )
        speed_mps = cls._to_float(vehicle.get("speed_mps", 0.0))
        speed = vehicle.get("speed")
        if speed is None:
            speed = "{:.1f} km/h".format(speed_mps * 3.6)

        task_status = (
            vehicle.get("status")
            or vehicle.get("task_status")
            or "idle"
        )
        task_id = (
            vehicle.get("task")
            or vehicle.get("current_task_id")
            or "等待调度"
        )
        position_xyz = cls._normalize_position(
            vehicle.get("position_xyz") or vehicle.get("position")
        )
        target_position_xyz = cls._normalize_position(
            vehicle.get("target_position_xyz")
            or vehicle.get("target_position")
        )

        return {
            "id": vehicle_id,
            "name": vehicle.get("display_name", vehicle.get("name", vehicle_id)),
            "type": (
                vehicle.get("type")
                or vehicle.get("equipment_type")
                or "-"
            ),
            "status": task_status,
            "speed": speed,
            "speed_mps": speed_mps,
            "position": cls._format_position(position_xyz),
            "position_xyz": position_xyz,
            "yaw_deg": cls._to_float(vehicle.get("yaw_deg", 0.0)),
            "target_position_xyz": target_position_xyz,
            "route_points": cls._normalize_points(
                vehicle.get("route_points", [])
            ),
            "trajectory_points": cls._normalize_points(
                vehicle.get("trajectory_points", [])
            ),
            "task": task_id,
            "health": vehicle.get("health", "unknown"),
            "communication": vehicle.get("communication", "online"),
            "source": vehicle.get("source", "AI"),
            "available": vehicle.get("available", True),
            "actor_id": vehicle.get("actor_id"),
            "battery_percent": vehicle.get("battery_percent", 100.0),
            "timestamp": vehicle.get("timestamp"),
        }

    def sync_snapshot(self, payload):
        vehicles = payload.get("vehicles")
        if isinstance(vehicles, list):
            self.vehicles = [
                self._normalize_vehicle(item)
                for item in vehicles
                if isinstance(item, dict)
            ]

        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            self.tasks = [
                dict(item)
                for item in tasks
                if isinstance(item, dict)
            ]

        run_id = payload.get("run_id")
        if run_id:
            self.run_id = str(run_id)

        map_name = payload.get("map_name")
        if map_name:
            self.map_name = str(map_name)

        environment = payload.get("environment")
        if isinstance(environment, dict):
            merged_environment = dict(self.environment)
            merged_environment.update(environment)
            self.environment = merged_environment
            if environment.get("map_name"):
                self.map_name = str(environment.get("map_name"))

        risk = payload.get("risk")
        if isinstance(risk, dict):
            normalized_risk = dict(risk)
            normalized_risk.setdefault(
                "assessment",
                normalized_risk.get("reason", []),
            )
            self.risk = normalized_risk

        decision = payload.get("decision")
        if isinstance(decision, dict):
            self.decision = dict(decision)

        monitoring = payload.get("monitoring")
        if isinstance(monitoring, dict):
            merged_monitoring = dict(self.monitoring)
            merged_monitoring.update(monitoring)
            merged_monitoring["updated_at"] = datetime.now().isoformat()
            self.monitoring = merged_monitoring

        assignments = payload.get("assignments")
        if isinstance(assignments, list) and assignments:
            self.decision = {
                "status": "SCHEDULED",
                "assignment_count": len(assignments),
                "assignments": assignments,
            }

        event = payload.get("event")
        if isinstance(event, dict):
            self.add_event(
                event.get("type", "system"),
                event.get("message", ""),
            )

    def update_vehicle(self, vehicle_id, data):
        for vehicle in self.vehicles:
            if vehicle.get("id") == vehicle_id:
                vehicle.update(data)
                return True
        return False

    def stop_vehicle(self, vehicle_id):
        success = self.update_vehicle(
            vehicle_id,
            {
                "status": "EMERGENCY_STOP",
                "speed": "0.0 km/h",
                "speed_mps": 0.0,
            },
        )
        if success:
            self.add_event(
                "Human Operator",
                "{} 紧急停止".format(vehicle_id),
            )
        return success

    def pause_vehicle(self, vehicle_id):
        success = self.update_vehicle(
            vehicle_id,
            {
                "status": "PAUSED",
                "speed": "0.0 km/h",
                "speed_mps": 0.0,
            },
        )
        if success:
            self.add_event(
                "Human Operator",
                "{} 暂停当前任务".format(vehicle_id),
            )
        return success

    @staticmethod
    def _command_timestamp():
        return datetime.now().isoformat(timespec="milliseconds")

    def add_command(self, command):
        """把UI控制请求写入待执行队列，不伪造CARLA执行结果。"""

        if not isinstance(command, dict):
            raise ValueError("command必须是JSON对象")

        action = str(command.get("action", "")).strip()
        if not action:
            raise ValueError("command.action不能为空")

        vehicle_required_actions = {
            "pause_vehicle",
            "resume_vehicle",
            "emergency_stop",
            "manual_dispatch",
        }
        vehicle_id = command.get("vehicle_id")
        if action in vehicle_required_actions and not vehicle_id:
            raise ValueError("{}需要vehicle_id".format(action))

        now = self._command_timestamp()
        item = dict(command)
        item.update(
            {
                "command_id": "cmd-{}".format(uuid4().hex[:12]),
                "action": action,
                "source": command.get("source", "human_operator"),
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "executed_at": None,
                "message": "等待CARLA运行进程执行",
                "result": None,
            }
        )

        with self._command_lock:
            self.commands.append(item)
            if len(self.commands) > 500:
                self.commands = self.commands[-500:]

        self.add_event(
            "command_queued",
            "{} 已进入命令队列：{}".format(
                item.get("vehicle_id", "system"),
                action,
            ),
        )
        return dict(item)

    def get_commands(self, limit=100):
        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            safe_limit = 100

        with self._command_lock:
            return [
                dict(item)
                for item in self.commands[-safe_limit:]
            ]

    def get_pending_commands(self, limit=20):
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            safe_limit = 20

        with self._command_lock:
            pending = [
                dict(item)
                for item in self.commands
                if item.get("status") == "pending"
            ]
        return pending[:safe_limit]

    def acknowledge_command(
        self,
        command_id,
        status,
        message=None,
        result=None,
    ):
        allowed = {"executing", "succeeded", "failed", "rejected"}
        if status not in allowed:
            raise ValueError(
                "不支持的命令状态：{}".format(status)
            )

        updated = None
        with self._command_lock:
            for item in self.commands:
                if item.get("command_id") != command_id:
                    continue
                item["status"] = status
                item["updated_at"] = self._command_timestamp()
                if status in {"succeeded", "failed", "rejected"}:
                    item["executed_at"] = item["updated_at"]
                if message is not None:
                    item["message"] = str(message)
                if result is not None:
                    item["result"] = result
                updated = dict(item)
                break

        if updated is None:
            return None

        self.add_event(
            "command_{}".format(status),
            "{} {}：{}".format(
                updated.get("vehicle_id", "system"),
                updated.get("action", "command"),
                updated.get("message", status),
            ),
        )
        return updated

    def get_agents(self):
        return {
            "agents": self.agents,
            "environment": self.environment,
            "risk": self.risk,
            "decision": self.decision,
            "monitoring": self.monitoring,
        }

    def get_monitoring(self):
        return dict(self.monitoring)

    def get_dispatch(self):
        return {
            "tasks": self.tasks,
            "run_id": self.run_id,
        }

    def _map_points(self):
        points = []

        map_bounds = self.environment.get("map_bounds")
        if isinstance(map_bounds, dict):
            points.extend(
                [
                    {
                        "x": map_bounds.get("min_x", -50.0),
                        "y": map_bounds.get("min_y", -50.0),
                    },
                    {
                        "x": map_bounds.get("max_x", 50.0),
                        "y": map_bounds.get("max_y", 50.0),
                    },
                ]
            )

        for zone in self.environment.get("zones", []):
            if isinstance(zone, dict):
                position = self._normalize_position(zone.get("position"))
                if position is not None:
                    points.append(position)

        for area in self.environment.get("monitoring_areas", []):
            if isinstance(area, dict):
                position = self._normalize_position(
                    area.get("center_position")
                )
                if position is not None:
                    points.append(position)

        for station in self.environment.get(
            "fixed_monitoring_stations", []
        ):
            if isinstance(station, dict):
                position = self._normalize_position(station.get("position"))
                if position is not None:
                    points.append(position)

        for vehicle in self.vehicles:
            for key in ("position_xyz", "target_position_xyz"):
                position = vehicle.get(key)
                if isinstance(position, dict):
                    points.append(position)

        return points

    def _map_bounds(self):
        configured = self.environment.get("map_bounds")
        if isinstance(configured, dict):
            return {
                "min_x": self._to_float(configured.get("min_x", -50.0)),
                "max_x": self._to_float(configured.get("max_x", 50.0)),
                "min_y": self._to_float(configured.get("min_y", -50.0)),
                "max_y": self._to_float(configured.get("max_y", 50.0)),
            }

        points = self._map_points()
        if not points:
            return {
                "min_x": -50.0,
                "max_x": 50.0,
                "min_y": -50.0,
                "max_y": 50.0,
            }

        xs = [self._to_float(item.get("x", 0.0)) for item in points]
        ys = [self._to_float(item.get("y", 0.0)) for item in points]
        width = max(max(xs) - min(xs), 20.0)
        height = max(max(ys) - min(ys), 20.0)
        padding = max(width, height) * 0.15 + 8.0
        return {
            "min_x": min(xs) - padding,
            "max_x": max(xs) + padding,
            "min_y": min(ys) - padding,
            "max_y": max(ys) + padding,
        }

    def get_map_state(self):
        targets = []
        target_keys = set()
        routes = []
        trajectories = []

        for vehicle in self.vehicles:
            target = vehicle.get("target_position_xyz")
            if isinstance(target, dict):
                task_id = vehicle.get("task")
                key = (task_id, target.get("x"), target.get("y"))
                if key not in target_keys:
                    target_keys.add(key)
                    targets.append(
                        {
                            "task_id": task_id,
                            "vehicle_id": vehicle.get("id"),
                            "position": target,
                        }
                    )

            route_points = vehicle.get("route_points", [])
            if isinstance(route_points, list) and len(route_points) >= 2:
                routes.append(
                    {
                        "vehicle_id": vehicle.get("id"),
                        "task_id": vehicle.get("task"),
                        "status": vehicle.get("status"),
                        "points": route_points,
                    }
                )

            trajectory_points = vehicle.get("trajectory_points", [])
            if (
                isinstance(trajectory_points, list)
                and len(trajectory_points) >= 2
            ):
                trajectories.append(
                    {
                        "vehicle_id": vehicle.get("id"),
                        "points": trajectory_points,
                    }
                )

        map_vehicles = []
        for vehicle in self.vehicles:
            position = vehicle.get("position_xyz")
            if not isinstance(position, dict):
                continue
            map_vehicles.append(
                {
                    "id": vehicle.get("id"),
                    "name": vehicle.get("name"),
                    "type": vehicle.get("type"),
                    "position": position,
                    "yaw_deg": vehicle.get("yaw_deg", 0.0),
                    "speed_mps": vehicle.get("speed_mps", 0.0),
                    "speed": vehicle.get("speed", "-"),
                    "status": vehicle.get("status", "idle"),
                    "health": vehicle.get("health", "unknown"),
                    "task_id": vehicle.get("task"),
                    "communication": vehicle.get(
                        "communication", "online"
                    ),
                }
            )

        road_segments = self.environment.get("road_segments", [])
        zones = self.environment.get("zones", [])
        risk_areas = self.environment.get("risk_areas", [])
        monitoring_areas = self.environment.get("monitoring_areas", [])
        fixed_monitoring_stations = self.environment.get(
            "fixed_monitoring_stations", []
        )

        return {
            "map_name": self.map_name,
            "run_id": self.run_id,
            "bounds": self._map_bounds(),
            "roads": (
                road_segments if isinstance(road_segments, list) else []
            ),
            "zones": zones if isinstance(zones, list) else [],
            "vehicles": map_vehicles,
            "targets": targets,
            "routes": routes,
            "trajectories": trajectories,
            "risk_areas": (
                risk_areas if isinstance(risk_areas, list) else []
            ),
            "monitoring_areas": (
                monitoring_areas
                if isinstance(monitoring_areas, list)
                else []
            ),
            "fixed_monitoring_stations": (
                fixed_monitoring_stations
                if isinstance(fixed_monitoring_stations, list)
                else []
            ),
            "updated_at": datetime.now().isoformat(),
        }

    def get_state(self):
        return {
            "vehicles": self.vehicles,
            "tasks": self.tasks,
            "events": self.events,
            "agents": self.agents,
            "environment": self.environment,
            "risk": self.risk,
            "decision": self.decision,
            "monitoring": self.monitoring,
            "run_id": self.run_id,
            "map_name": self.map_name,
            "commands": self.get_commands(limit=100),
        }


runtime = RuntimeState()
