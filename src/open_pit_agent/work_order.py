"""Auditable work-order lifecycle linked to risk events and tasks."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .models import Task, utc_now
from .risk import RiskAction, RiskAssessment


TERMINAL_STATUSES = {"closed", "escalated", "cancelled"}
ALLOWED_TRANSITIONS = {
    "pending": {"assigned", "cancelled"},
    "assigned": {"in_progress", "cancelled"},
    "in_progress": {"pending_review", "escalated"},
    "pending_review": {"closed", "escalated"},
    "closed": set(),
    "escalated": set(),
    "cancelled": set(),
}


class WorkOrderError(RuntimeError):
    """Raised when a work order attempts an invalid state transition."""


@dataclass(frozen=True)
class WorkOrderTransition:
    from_status: Optional[str]
    to_status: str
    tick: int
    actor: str
    reason: str
    work_order_id: Optional[str] = None
    task_id: Optional[str] = None
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkOrder:
    work_order_id: str
    order_type: str
    title: str
    risk_event_id: str
    risk_level: str
    zone_id: str
    task_id: str
    status: str
    created_tick: int
    auto_review_delay_ticks: int
    assigned_vehicle_id: Optional[str] = None
    assigned_tick: Optional[int] = None
    started_tick: Optional[int] = None
    action_completed_tick: Optional[int] = None
    pending_review_tick: Optional[int] = None
    reviewed_tick: Optional[int] = None
    closed_tick: Optional[int] = None
    review_result: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    history: List[WorkOrderTransition] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WorkOrderManager:
    def __init__(self) -> None:
        self._orders: Dict[str, WorkOrder] = {}
        self._order_ids_by_task: Dict[str, str] = {}

    def create_from_risk(
        self,
        assessment: RiskAssessment,
        task: Task,
        action: RiskAction,
        tick: int,
    ) -> WorkOrder:
        work_order_id = "wo-{}".format(task.task_id)
        if work_order_id in self._orders:
            raise WorkOrderError(
                "Duplicate work order: {}".format(work_order_id)
            )
        transition = WorkOrderTransition(
            from_status=None,
            to_status="pending",
            tick=tick,
            actor="risk_engine",
            reason="created_from_{}_risk".format(assessment.level),
            work_order_id=work_order_id,
            task_id=task.task_id,
        )
        order = WorkOrder(
            work_order_id=work_order_id,
            order_type=action.work_order_type,
            title="{}:{}:{}".format(
                assessment.zone_id,
                assessment.level,
                action.work_order_type,
            ),
            risk_event_id=assessment.assessment_id,
            risk_level=assessment.level,
            zone_id=assessment.zone_id,
            task_id=task.task_id,
            status="pending",
            created_tick=tick,
            auto_review_delay_ticks=action.auto_review_delay_ticks,
            history=[transition],
        )
        self._orders[work_order_id] = order
        self._order_ids_by_task[task.task_id] = work_order_id
        return order

    def assign(
        self, task_id: str, vehicle_id: str, tick: int
    ) -> WorkOrderTransition:
        order = self._order_for_task(task_id)
        order.assigned_vehicle_id = vehicle_id
        order.assigned_tick = tick
        return self._transition(
            order,
            "assigned",
            tick,
            actor="scheduler",
            reason="risk_task_assigned_to_{}".format(vehicle_id),
        )

    def process_adapter_events(
        self, events: Sequence[Dict[str, Any]]
    ) -> List[WorkOrderTransition]:
        transitions = []
        for event in events:
            event_type = str(event["event_type"])
            payload = event["payload"]
            task_id = payload.get("task_id")
            if not task_id or task_id not in self._order_ids_by_task:
                continue
            order = self._order_for_task(str(task_id))
            tick = int(event["tick"])
            if event_type == "task_started":
                order.started_tick = tick
                transitions.append(
                    self._transition(
                        order,
                        "in_progress",
                        tick,
                        actor="carla_adapter",
                        reason="linked_task_started",
                    )
                )
            elif event_type == "task_completed":
                order.action_completed_tick = tick
                order.pending_review_tick = tick
                transitions.append(
                    self._transition(
                        order,
                        "pending_review",
                        tick,
                        actor="carla_adapter",
                        reason="linked_task_completed",
                    )
                )
            elif event_type == "task_timed_out":
                transitions.append(
                    self._transition(
                        order,
                        "escalated",
                        tick,
                        actor="carla_adapter",
                        reason="linked_task_timed_out",
                    )
                )
        return transitions

    def auto_review(self, tick: int) -> List[WorkOrderTransition]:
        transitions = []
        for order in self._orders.values():
            if (
                order.status != "pending_review"
                or order.pending_review_tick is None
                or tick - order.pending_review_tick
                < order.auto_review_delay_ticks
            ):
                continue
            order.reviewed_tick = tick
            order.closed_tick = tick
            order.review_result = (
                "approved_by_demo_rule_no_human_review"
            )
            transitions.append(
                self._transition(
                    order,
                    "closed",
                    tick,
                    actor="demo_auto_reviewer",
                    reason=order.review_result,
                )
            )
        return transitions

    def orders(self) -> List[WorkOrder]:
        return [
            self._orders[key] for key in sorted(self._orders)
        ]

    def all_terminal(self) -> bool:
        return bool(self._orders) and all(
            order.status in TERMINAL_STATUSES
            for order in self._orders.values()
        )

    def summary(self) -> Dict[str, Any]:
        orders = self.orders()
        counts = Counter(order.status for order in orders)
        closed = counts["closed"]
        return {
            "work_orders": [order.to_dict() for order in orders],
            "work_order_status_counts": dict(sorted(counts.items())),
            "work_order_count": len(orders),
            "work_order_close_rate": (
                round(closed / float(len(orders)), 4)
                if orders
                else 0.0
            ),
            "all_work_orders_terminal": self.all_terminal(),
            "work_order_metrics": [
                self._metrics(order) for order in orders
            ],
        }

    def _order_for_task(self, task_id: str) -> WorkOrder:
        try:
            order_id = self._order_ids_by_task[task_id]
            return self._orders[order_id]
        except KeyError as exc:
            raise WorkOrderError(
                "No work order linked to task {}".format(task_id)
            ) from exc

    def _transition(
        self,
        order: WorkOrder,
        new_status: str,
        tick: int,
        actor: str,
        reason: str,
    ) -> WorkOrderTransition:
        allowed = ALLOWED_TRANSITIONS.get(order.status, set())
        if new_status not in allowed:
            raise WorkOrderError(
                "Invalid work order transition {} -> {} for {}".format(
                    order.status, new_status, order.work_order_id
                )
            )
        transition = WorkOrderTransition(
            from_status=order.status,
            to_status=new_status,
            tick=tick,
            actor=actor,
            reason=reason,
            work_order_id=order.work_order_id,
            task_id=order.task_id,
        )
        order.status = new_status
        order.updated_at = transition.timestamp
        order.history.append(transition)
        return transition

    @staticmethod
    def _metrics(order: WorkOrder) -> Dict[str, Any]:
        return {
            "work_order_id": order.work_order_id,
            "risk_event_id": order.risk_event_id,
            "task_id": order.task_id,
            "created_tick": order.created_tick,
            "assigned_tick": order.assigned_tick,
            "started_tick": order.started_tick,
            "action_completed_tick": order.action_completed_tick,
            "closed_tick": order.closed_tick,
            "assignment_latency_ticks": (
                order.assigned_tick - order.created_tick
                if order.assigned_tick is not None
                else None
            ),
            "start_latency_ticks": (
                order.started_tick - order.created_tick
                if order.started_tick is not None
                else None
            ),
            "action_latency_ticks": (
                order.action_completed_tick - order.created_tick
                if order.action_completed_tick is not None
                else None
            ),
            "close_latency_ticks": (
                order.closed_tick - order.created_tick
                if order.closed_tick is not None
                else None
            ),
        }
