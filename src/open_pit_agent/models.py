"""Shared, simulator-independent domain models."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import sqrt
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float = 0.0

    def distance_to(self, other: "Position") -> float:
        return sqrt(
            (self.x - other.x) ** 2
            + (self.y - other.y) ** 2
            + (self.z - other.z) ** 2
        )


@dataclass
class VehicleState:
    vehicle_id: str
    display_name: str
    equipment_type: str
    role_name: str
    blueprint: str
    capabilities: List[str]
    position: Position
    yaw_deg: float = 0.0
    target_position: Optional[Position] = None
    route_points: List[Dict[str, float]] = field(default_factory=list)
    trajectory_points: List[Dict[str, float]] = field(default_factory=list)
    speed_mps: float = 0.0
    battery_percent: float = 100.0
    health: str = "healthy"
    available: bool = True
    actor_id: Optional[int] = None
    current_task_id: Optional[str] = None
    task_status: str = "idle"
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Task:
    task_id: str
    zone_id: str
    priority: int
    required_capabilities: List[str]
    status: str = "pending"
    assigned_vehicle_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    started_tick: Optional[int] = None
    completed_tick: Optional[int] = None
    attempt_count: int = 0
    last_distance_m: Optional[float] = None
    status_reason: Optional[str] = None
    task_type: str = "inspection"
    source_event_id: Optional[str] = None
    original_vehicle_id: Optional[str] = None
    handover_reason: Optional[str] = None
    handover_tick: Optional[int] = None
    recommended_vehicle_id: Optional[str] = None
    recommendation_reason: Optional[str] = None
    candidate_evaluations: List[Dict[str, Any]] = field(default_factory=list)
    preferred_vehicle_id: Optional[str] = None
    original_route: List[Dict[str, float]] = field(default_factory=list)
    completed_route: List[Dict[str, float]] = field(default_factory=list)
    remaining_route: List[Dict[str, float]] = field(default_factory=list)
    last_completed_waypoint: Optional[Dict[str, float]] = None
    safe_merge_point: Optional[Dict[str, float]] = None
    transfer_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        original_route = payload.pop("original_route", [])
        completed_route = payload.pop("completed_route", [])
        remaining_route = payload.pop("remaining_route", [])
        payload.update(
            {
                "original_route_checkpoint_count": len(original_route),
                "completed_route_checkpoint_count": len(completed_route),
                "remaining_route_checkpoint_count": len(remaining_route),
            }
        )
        return payload


@dataclass(frozen=True)
class Assignment:
    task_id: str
    zone_id: str
    vehicle_id: str
    score: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
