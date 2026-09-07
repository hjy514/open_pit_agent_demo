"""Minimal interface and execution contracts shared by Mock and CARLA."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import ZoneConfig
from ..models import Task, VehicleState, utc_now


EXECUTION_COMMAND_SCHEMA_VERSION = "openpit.execution-command.v1"
EXECUTION_FEEDBACK_SCHEMA_VERSION = "openpit.execution-feedback.v1"


@dataclass(frozen=True)
class ExecutionCommand:
    command_id: str
    action_type: str
    task_ids: Tuple[str, ...]
    assignments: Dict[str, str]
    issued_by: str
    safety_gate_status: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["task_ids"] = list(self.task_ids)
        payload["schema_version"] = EXECUTION_COMMAND_SCHEMA_VERSION
        return payload


@dataclass
class ExecutionFeedback:
    command_id: str
    phase: str
    status: str
    adapter_type: str
    physical_execution: bool
    measurement_status: str
    safety_gate_status: str
    task_states: List[Dict[str, Any]] = field(default_factory=list)
    vehicle_states: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = EXECUTION_FEEDBACK_SCHEMA_VERSION
        return payload


class EquipmentAdapter(ABC):
    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_states(self) -> Sequence[VehicleState]:
        raise NotImplementedError

    @abstractmethod
    def dispatch(self, tasks: Sequence[Task], zones: Sequence[ZoneConfig]) -> None:
        raise NotImplementedError

    @abstractmethod
    def inject_fault(self, vehicle_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class ExecutionManager:
    """Translate one common command into adapter calls and factual feedback."""

    def __init__(self, adapter: EquipmentAdapter,
                 physical_execution: bool = False) -> None:
        self.adapter = adapter
        self.physical_execution = bool(physical_execution)

    @property
    def measurement_status(self) -> str:
        return (
            "CARLA_MEASURED" if self.physical_execution
            else "STRUCTURAL_ONLY_NO_PHYSICS"
        )

    @staticmethod
    def _task_states(tasks: Sequence[Task]) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in tasks]

    def _vehicle_states(self) -> List[Dict[str, Any]]:
        output = []
        for item in self.adapter.list_states():
            converter = getattr(item, "to_dict", None)
            output.append(
                converter() if callable(converter)
                else dict(vars(item)) if hasattr(item, "__dict__")
                else {"raw_state": str(item)}
            )
        return output

    def _drain_events(self) -> List[Dict[str, Any]]:
        drain = getattr(self.adapter, "drain_events", None)
        return list(drain()) if callable(drain) else []

    @staticmethod
    def _validation_error(
        command: ExecutionCommand, tasks: Sequence[Task]
    ) -> Optional[str]:
        if command.action_type != "dispatch_tasks":
            return "unsupported execution action: {}".format(command.action_type)
        if command.safety_gate_status == "REJECTED":
            return "execution blocked by Safety Shield"
        tasks_by_id = {str(item.task_id): item for item in tasks}
        if set(command.task_ids) != set(tasks_by_id):
            return "execution command task set does not match supplied tasks"
        observed = {
            task_id: str(tasks_by_id[task_id].assigned_vehicle_id)
            for task_id in command.task_ids
        }
        if observed != {str(key): str(value)
                        for key, value in command.assignments.items()}:
            return "execution command assignments do not match task state"
        return None

    def dispatch(
        self, command: ExecutionCommand, tasks: Sequence[Task],
        zones: Sequence[ZoneConfig],
    ) -> ExecutionFeedback:
        error = self._validation_error(command, tasks)
        if error is not None:
            return self.observe(command, tasks, "dispatch", "REJECTED", error)
        try:
            self.adapter.dispatch(tasks, zones)
            return self.observe(command, tasks, "dispatch", "SUCCEEDED")
        except Exception as exc:
            return ExecutionFeedback(
                command_id=command.command_id, phase="dispatch", status="FAILED",
                adapter_type=type(self.adapter).__name__,
                physical_execution=self.physical_execution,
                measurement_status=self.measurement_status,
                safety_gate_status=command.safety_gate_status,
                task_states=self._task_states(tasks),
                error="{}: {}".format(type(exc).__name__, exc),
            )

    def observe(
        self, command: ExecutionCommand, tasks: Sequence[Task], phase: str,
        status: str, error: Optional[str] = None,
        events: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> ExecutionFeedback:
        return ExecutionFeedback(
            command_id=command.command_id, phase=str(phase), status=str(status),
            adapter_type=type(self.adapter).__name__,
            physical_execution=self.physical_execution,
            measurement_status=self.measurement_status,
            safety_gate_status=command.safety_gate_status,
            task_states=self._task_states(tasks),
            vehicle_states=self._vehicle_states(),
            events=(list(events) if events is not None else self._drain_events()),
            error=error,
        )
