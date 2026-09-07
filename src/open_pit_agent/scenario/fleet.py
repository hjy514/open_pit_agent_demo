"""Explicit fleet master/episode separation for offline scenario generation."""
from dataclasses import dataclass, asdict
from random import Random
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class VehicleMaster:
    """Stable equipment identity shared by all episodes."""

    vehicle_id: str
    display_name: str
    equipment_type: str
    blueprint: str
    capabilities: List[str]


@dataclass(frozen=True)
class EpisodeVehicle:
    """Per-episode assignment and availability snapshot."""

    vehicle_id: str
    role: str
    status: str
    spawn_point_index: Optional[int]
    available: bool
    active: bool
    in_traffic: bool
    health: str = "healthy"
    soc_percent: float = 100.0


@dataclass(frozen=True)
class FleetSnapshot:
    """Resolved fleet envelope plus episode-specific vehicle records."""

    seed: int
    total: int
    available: int
    active: int
    traffic: int
    vehicles: Tuple[EpisodeVehicle, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed": self.seed,
            "total": self.total,
            "available": self.available,
            "active": self.active,
            "traffic": self.traffic,
            "vehicles": [asdict(item) for item in self.vehicles],
        }


def resolve_fleet(
    masters: Sequence[VehicleMaster],
    total: int,
    available: int,
    active: int,
    traffic: int,
    seed: int,
    roles: Optional[Dict[str, str]] = None,
    spawn_points: Optional[Dict[str, int]] = None,
) -> FleetSnapshot:
    """Resolve a deterministic fleet snapshot without CARLA or database access."""
    if total != len(masters):
        raise ValueError("total must match the supplied Vehicle Master records")
    if not (0 <= traffic <= active <= available <= total):
        raise ValueError("fleet counts must satisfy traffic <= active <= available <= total")
    rng = Random(int(seed))
    ids = [item.vehicle_id for item in masters]

    def choose(pool: Iterable[str], count: int) -> set:
        values = list(pool)
        rng.shuffle(values)
        return set(values[:count])

    available_ids = choose(ids, available)
    active_ids = choose([item for item in ids if item in available_ids], active)
    traffic_ids = choose([item for item in ids if item in active_ids], traffic)
    role_map = dict(roles or {})
    result: List[EpisodeVehicle] = []
    for master in masters:
        is_available = master.vehicle_id in available_ids
        is_active = master.vehicle_id in active_ids
        in_traffic = master.vehicle_id in traffic_ids
        status = "unavailable" if not is_available else "in_traffic" if in_traffic else "active" if is_active else "idle"
        result.append(EpisodeVehicle(
            vehicle_id=master.vehicle_id,
            role=str(role_map.get(master.vehicle_id, "unassigned")),
            status=status,
            spawn_point_index=(spawn_points or {}).get(master.vehicle_id),
            available=is_available,
            active=is_active,
            in_traffic=in_traffic,
        ))
    return FleetSnapshot(int(seed), total, available, active, traffic, tuple(result))


def snapshot_from_episode(episode: Any) -> FleetSnapshot:
    """Convert the legacy ConcreteEpisode snapshot into the explicit model."""
    records = []
    for item in episode.vehicles:
        records.append(EpisodeVehicle(
            vehicle_id=item.vehicle_id,
            role=getattr(item, "initial_role", "unassigned"),
            status=getattr(item, "initial_status", "idle"),
            spawn_point_index=getattr(item, "spawn_point_index", None),
            available=bool(item.available),
            active=bool(item.active),
            in_traffic=bool(item.in_traffic),
        ))
    snapshot = getattr(episode, "fleet_snapshot", {}) or {}
    return FleetSnapshot(
        seed=int(episode.seed),
        total=int(snapshot.get("total_vehicles", len(records))),
        available=int(snapshot.get("available_vehicles", sum(item.available for item in records))),
        active=int(snapshot.get("active_vehicles", sum(item.active for item in records))),
        traffic=int(snapshot.get("traffic_vehicles", sum(item.in_traffic for item in records))),
        vehicles=tuple(records),
    )


def vehicle_state_snapshot(states: Sequence[Any]) -> List[Dict[str, Any]]:
    """Compact runtime state view for structural-run results.

    Unlike ``snapshot_from_episode``, this is taken from the adapter at the
    requested instant and therefore can show a fault or task reassignment.
    """
    return [{
        "vehicle_id": state.vehicle_id, "role_name": state.role_name,
        "health": state.health, "available": state.available,
        "task_status": state.task_status, "current_task_id": state.current_task_id,
    } for state in sorted(states, key=lambda item: item.vehicle_id)]
