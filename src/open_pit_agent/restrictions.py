"""Auditable policy-layer road restrictions triggered by risk events."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Sequence

from .models import Task, utc_now
from .risk import RestrictionAction, RiskAssessment


@dataclass(frozen=True)
class RoadRestriction:
    restriction_id: str
    risk_event_id: str
    risk_level: str
    zone_id: str
    road_segment_id: str
    allowed_task_types: List[str]
    activated_tick: int
    status: str = "active"
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if not payload["created_at"]:
            payload["created_at"] = utc_now()
        return payload


class RestrictionRegistry:
    """Track closures without claiming simulator-level route avoidance."""

    def __init__(self) -> None:
        self._restrictions: Dict[str, RoadRestriction] = {}

    def activate(
        self,
        assessment: RiskAssessment,
        action: RestrictionAction,
        tick: int,
    ) -> RoadRestriction:
        restriction_id = "{}-{}".format(
            action.restriction_id_prefix,
            assessment.assessment_id,
        )
        existing = self._restrictions.get(restriction_id)
        if existing is not None:
            return existing
        restriction = RoadRestriction(
            restriction_id=restriction_id,
            risk_event_id=assessment.assessment_id,
            risk_level=assessment.level,
            zone_id=action.zone_id,
            road_segment_id=action.road_segment_id,
            allowed_task_types=list(action.allowed_task_types),
            activated_tick=tick,
            created_at=utc_now(),
        )
        self._restrictions[restriction_id] = restriction
        return restriction

    def apply_to_tasks(
        self, tasks: Sequence[Task]
    ) -> List[str]:
        cancelled = []
        for task in tasks:
            if task.status in {
                "completed",
                "timed_out",
                "cancelled",
            }:
                continue
            for restriction in self._restrictions.values():
                if (
                    restriction.status == "active"
                    and task.zone_id == restriction.zone_id
                    and task.task_type
                    not in restriction.allowed_task_types
                ):
                    task.status = "cancelled"
                    task.assigned_vehicle_id = None
                    task.updated_at = utc_now()
                    task.status_reason = (
                        "cancelled_by_road_restriction"
                    )
                    cancelled.append(task.task_id)
                    break
        return cancelled

    def summary(self) -> Dict[str, Any]:
        restrictions = [
            self._restrictions[key].to_dict()
            for key in sorted(self._restrictions)
        ]
        return {
            "road_restrictions": restrictions,
            "active_road_restriction_count": sum(
                item["status"] == "active"
                for item in restrictions
            ),
            "route_avoidance_enforced": False,
            "restriction_scope": (
                "policy_layer_only_town03_proxy"
            ),
        }
