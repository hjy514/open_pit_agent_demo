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

    def rank_candidates(
        self,
        task: Task,
        vehicles: Sequence[VehicleState],
        zones: Sequence[ZoneConfig],
        active_tasks: Sequence[Task] = (),
        excluded_vehicle_ids: Iterable[str] = (),
    ) -> List[Assignment]:
        """Rank capable takeover vehicles without mutating task state."""

        zone_by_id = {zone.zone_id: zone for zone in zones}
        zone = zone_by_id[task.zone_id]
        excluded = set(excluded_vehicle_ids)
        loads = Counter(
            item.assigned_vehicle_id
            for item in active_tasks
            if item.assigned_vehicle_id
            and item.status in {"assigned", "executing"}
        )
        required = set(task.required_capabilities)
        ranked = []
        for vehicle in vehicles:
            if vehicle.vehicle_id in excluded:
                continue
            if not vehicle.available or vehicle.health != "healthy":
                continue
            if not required.issubset(set(vehicle.capabilities)):
                continue
            distance = vehicle.position.distance_to(zone.mock_position)
            load = loads[vehicle.vehicle_id]
            imitation_bonus = self.experience_preferences.get(
                (task.task_type, vehicle.vehicle_id), 0.0
            )
            score = distance + load * self.load_penalty - imitation_bonus
            ranked.append(
                Assignment(
                    task_id=task.task_id,
                    zone_id=task.zone_id,
                    vehicle_id=vehicle.vehicle_id,
                    score=round(score, 3),
                    reason=(
                        "capabilities matched; distance={:.2f}m; "
                        "active_load={}; imitation_bonus={:.2f}m"
                    ).format(distance, load, imitation_bonus),
                )
            )
        return sorted(
            ranked,
            key=lambda item: (item.score, item.vehicle_id),
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

            preferred_candidates = [
                candidate
                for candidate in candidates
                if candidate[1] == task.preferred_vehicle_id
            ]
            if preferred_candidates:
                candidates = preferred_candidates

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
                        "{}capabilities matched; distance={:.2f}m; "
                        "prior_load={}; imitation_bonus={:.2f}m"
                    ).format(
                        (
                            "preferred initial vehicle; "
                            if selected.vehicle_id
                            == task.preferred_vehicle_id
                            else ""
                        ),
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
            preferred_vehicle_id=zone.preferred_vehicle_id,
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


def release_hazard_affected_tasks(
    tasks: Sequence[Task], affected_vehicle_id: str, tick: int
) -> List[str]:
    """Release unfinished work for takeover after a hazard safe hold."""

    released = []
    for task in tasks:
        if (
            task.assigned_vehicle_id == affected_vehicle_id
            and task.status not in {"completed", "cancelled", "timed_out"}
        ):
            task.original_vehicle_id = affected_vehicle_id
            task.assigned_vehicle_id = None
            task.status = "pending"
            task.updated_at = utc_now()
            task.started_at = None
            task.started_tick = None
            task.last_distance_m = None
            task.handover_reason = "released_after_slope_hazard_safe_hold"
            task.handover_tick = int(tick)
            task.status_reason = "awaiting_hazard_task_takeover"
            released.append(task.task_id)
    return released
