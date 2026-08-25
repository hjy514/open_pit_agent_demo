"""Deterministic capability-aware baseline scheduler."""

from collections import Counter
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .config import ZoneConfig
from .models import Assignment, Task, VehicleState, utc_now


class SchedulingError(RuntimeError):
    """Raised when one or more tasks have no eligible vehicle."""


class BaselineScheduler:
    """Assign tasks using capability, distance, priority and current load."""

    def __init__(
        self,
        load_penalty: float = 1000.0,
        experience_preferences: Dict[Tuple[str, str], float] = None,
    ) -> None:
        self.load_penalty = float(load_penalty)
        self.experience_preferences = dict(
            experience_preferences or {}
        )

    def assign(
        self,
        tasks: Sequence[Task],
        vehicles: Sequence[VehicleState],
        zones: Sequence[ZoneConfig],
        excluded_vehicle_ids: Iterable[str] = (),
    ) -> List[Assignment]:
        zone_by_id: Dict[str, ZoneConfig] = {zone.zone_id: zone for zone in zones}
        excluded: Set[str] = set(excluded_vehicle_ids)
        loads = Counter(
            task.assigned_vehicle_id
            for task in tasks
            if task.assigned_vehicle_id and task.status in {"assigned", "executing"}
        )
        assignments: List[Assignment] = []

        ordered_tasks = sorted(
            tasks,
            key=lambda task: (-task.priority, task.created_at, task.task_id),
        )
        for task in ordered_tasks:
            if task.status in {"completed", "timed_out", "cancelled"}:
                continue
            if task.assigned_vehicle_id:
                assigned = next(
                    (
                        vehicle
                        for vehicle in vehicles
                        if vehicle.vehicle_id == task.assigned_vehicle_id
                    ),
                    None,
                )
                if assigned and assigned.available and assigned.health == "healthy":
                    continue

            zone = zone_by_id[task.zone_id]
            candidates: List[Tuple[float, str, VehicleState]] = []
            required = set(task.required_capabilities)
            for vehicle in vehicles:
                if vehicle.vehicle_id in excluded:
                    continue
                if not vehicle.available or vehicle.health != "healthy":
                    continue
                if not required.issubset(set(vehicle.capabilities)):
                    continue
                distance = vehicle.position.distance_to(zone.mock_position)
                imitation_bonus = self.experience_preferences.get(
                    (task.task_type, vehicle.vehicle_id), 0.0
                )
                score = (
                    distance
                    + loads[vehicle.vehicle_id] * self.load_penalty
                    - imitation_bonus
                )
                candidates.append((score, vehicle.vehicle_id, vehicle))

            if not candidates:
                raise SchedulingError(
                    "No eligible vehicle for task {} requiring {}".format(
                        task.task_id, sorted(required)
                    )
                )

            score, _, selected = min(candidates, key=lambda item: (item[0], item[1]))
            task.assigned_vehicle_id = selected.vehicle_id
            task.status = "assigned"
            task.updated_at = utc_now()
            task.status_reason = "assigned_by_scheduler"
            loads[selected.vehicle_id] += 1
            assignments.append(
                Assignment(
                    task_id=task.task_id,
                    zone_id=task.zone_id,
                    vehicle_id=selected.vehicle_id,
                    score=round(score, 3),
                    reason=(
                        "capabilities matched; distance={:.2f}m; "
                        "prior_load={}; imitation_bonus={:.2f}m"
                    ).format(
                        selected.position.distance_to(zone.mock_position),
                        loads[selected.vehicle_id] - 1,
                        self.experience_preferences.get(
                            (task.task_type, selected.vehicle_id),
                            0.0,
                        ),
                    ),
                )
            )

        return assignments


def tasks_from_zones(zones: Sequence[ZoneConfig]) -> List[Task]:
    return [
        Task(
            task_id="inspect-{}".format(zone.zone_id),
            zone_id=zone.zone_id,
            priority=zone.priority,
            required_capabilities=list(zone.required_capabilities),
            task_type="routine_inspection",
        )
        for zone in zones
        if zone.initial_task
    ]


def release_failed_vehicle_tasks(
    tasks: Sequence[Task], failed_vehicle_id: str
) -> List[str]:
    released = []
    for task in tasks:
        if (
            task.assigned_vehicle_id == failed_vehicle_id
            and task.status not in {"completed", "cancelled"}
        ):
            task.assigned_vehicle_id = None
            task.status = "pending"
            task.updated_at = utc_now()
            task.started_at = None
            task.started_tick = None
            task.last_distance_m = None
            task.status_reason = "released_after_vehicle_fault"
            released.append(task.task_id)
    return released
