"""Minimal interface shared by Mock and CARLA adapters."""

from abc import ABC, abstractmethod
from typing import Sequence

from ..config import ZoneConfig
from ..models import Task, VehicleState


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

