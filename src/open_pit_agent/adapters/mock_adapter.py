"""Deterministic in-memory equipment adapter."""

from copy import deepcopy
from typing import Dict, Sequence

from .base import EquipmentAdapter
from ..config import ScenarioConfig, ZoneConfig
from ..models import Task, VehicleState, utc_now


class MockAdapter(EquipmentAdapter):
    def __init__(self, config: ScenarioConfig) -> None:
        self.config = config
        self.connected = False
        self._states: Dict[str, VehicleState] = {
            item.vehicle_id: VehicleState(
                vehicle_id=item.vehicle_id,
                display_name=item.display_name,
                equipment_type=item.equipment_type,
                role_name=item.role_name,
                blueprint=item.blueprint,
                capabilities=list(item.capabilities),
                position=item.mock_position,
            )
            for item in config.vehicles
        }

    def connect(self) -> None:
        self.connected = True

    def list_states(self) -> Sequence[VehicleState]:
        self._require_connected()
        return [deepcopy(self._states[key]) for key in sorted(self._states)]

    def dispatch(self, tasks: Sequence[Task], zones: Sequence[ZoneConfig]) -> None:
        self._require_connected()
        tasks_by_vehicle: Dict[str, list] = {}
        for task in tasks:
            if task.assigned_vehicle_id:
                tasks_by_vehicle.setdefault(task.assigned_vehicle_id, []).append(task)

        for vehicle_id, state in self._states.items():
            assigned = sorted(
                tasks_by_vehicle.get(vehicle_id, []),
                key=lambda task: (-task.priority, task.task_id),
            )
            if not state.available:
                state.current_task_id = None
                state.task_status = "fault"
            elif assigned:
                state.current_task_id = assigned[0].task_id
                state.task_status = "assigned"
            else:
                state.current_task_id = None
                state.task_status = "idle"
            state.timestamp = utc_now()

    def inject_fault(self, vehicle_id: str) -> None:
        self._require_connected()
        try:
            state = self._states[vehicle_id]
        except KeyError as exc:
            raise KeyError("Unknown vehicle_id: {}".format(vehicle_id)) from exc
        state.available = False
        state.health = "fault"
        state.speed_mps = 0.0
        state.current_task_id = None
        state.task_status = "fault"
        state.timestamp = utc_now()

    def complete_tasks(self, tasks: Sequence[Task]) -> None:
        """Reflect structural task completion in the mock runtime state only."""
        self._require_connected()
        completed_by_vehicle = {
            task.assigned_vehicle_id for task in tasks
            if task.assigned_vehicle_id and task.status == "completed"
        }
        for vehicle_id in completed_by_vehicle:
            state = self._states[vehicle_id]
            if state.available:
                state.current_task_id = None
                state.task_status = "completed"
                state.timestamp = utc_now()

    def close(self) -> None:
        self.connected = False

    def _require_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("MockAdapter is not connected")
