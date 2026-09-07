"""Small coordinator for one state-to-feedback decision cycle.

Domain algorithms stay in their existing modules.  This module only defines
their execution order and makes feedback update the shared RuntimeState.
"""

from copy import deepcopy
from typing import Any, Callable, Dict, List

from .runtime_state import RuntimeState


CLOSED_LOOP_CYCLE_SCHEMA_VERSION = "openpit.closed-loop-cycle.v1"
APPROVED_SAFETY_STATUSES = {"APPROVED", "MODIFIED", "PASS"}


class ClosedLoopCoordinator:
    """Run one deterministic orchestration cycle using injected stages."""

    def __init__(
        self,
        state_manager: RuntimeState,
        risk_stage: Callable[[Dict[str, Any]], Dict[str, Any]],
        decision_stage: Callable[[Dict[str, Any]], Dict[str, Any]],
        scheduling_stage: Callable[[Dict[str, Any]], Dict[str, Any]],
        planning_stage: Callable[[Dict[str, Any]], Dict[str, Any]],
        safety_stage: Callable[[Dict[str, Any]], Dict[str, Any]],
        execution_stage: Callable[[Dict[str, Any]], Any],
    ) -> None:
        self.state_manager = state_manager
        self._stages = (
            ("risk", risk_stage),
            ("decision", decision_stage),
            ("scheduling", scheduling_stage),
            ("planning", planning_stage),
            ("safety", safety_stage),
        )
        self.execution_stage = execution_stage

    @staticmethod
    def _require_mapping(stage_name: str, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("{} stage must return an object".format(stage_name))
        return dict(value)

    @staticmethod
    def _feedback_items(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, dict) and isinstance(
                value.get("execution_feedback"), list):
            value = value["execution_feedback"]
        elif isinstance(value, dict):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                "execution stage must return feedback object(s)"
            )
        output = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("execution feedback must be an object")
            output.append(dict(item))
        if not output:
            raise ValueError("execution stage returned no feedback")
        return output

    def run_cycle(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(initial_state, dict):
            raise ValueError("initial_state must be an object")
        sync_payload = (
            deepcopy(initial_state)
            if isinstance(initial_state.get("world_state"), dict)
            else {"world_state": deepcopy(initial_state)}
        )
        self.state_manager.sync_snapshot(sync_payload)
        state_before = self.state_manager.get_world_state()
        revision_before = self.state_manager.state_revision
        cycle_id = "cycle:{}".format(revision_before)
        context = {
            "world_state": deepcopy(state_before),
            "stage_results": {},
        }
        trace = [{
            "stage": "state", "status": "READY",
            "state_revision": revision_before,
        }]

        for stage_name, handler in self._stages:
            result = self._require_mapping(stage_name, handler(context))
            context["stage_results"][stage_name] = result
            context[stage_name] = result
            trace.append({"stage": stage_name, "status": "COMPLETED"})

        safety_status = str(
            context["stage_results"]["safety"].get("status") or "UNKNOWN"
        ).upper()
        if safety_status not in APPROVED_SAFETY_STATUSES:
            trace.append({
                "stage": "execution", "status": "BLOCKED_BY_SAFETY"
            })
            return {
                "schema_version": CLOSED_LOOP_CYCLE_SCHEMA_VERSION,
                "cycle_id": cycle_id,
                "status": "BLOCKED_BY_SAFETY",
                "state_before": state_before,
                "stage_results": deepcopy(context["stage_results"]),
                "execution_feedback": [],
                "next_state": self.state_manager.get_world_state(),
                "revision_before": revision_before,
                "revision_after": self.state_manager.state_revision,
                "trace": trace,
            }

        feedback_items = self._feedback_items(self.execution_stage(context))
        applied_count = 0
        for feedback in feedback_items:
            if self.state_manager.apply_execution_feedback(feedback):
                applied_count += 1
        trace.append({
            "stage": "execution", "status": "COMPLETED",
            "feedback_count": len(feedback_items),
        })
        trace.append({
            "stage": "feedback", "status": "APPLIED",
            "applied_count": applied_count,
            "state_revision": self.state_manager.state_revision,
        })
        feedback_statuses = {
            str(item.get("status") or "UNKNOWN").upper()
            for item in feedback_items
        }
        cycle_status = (
            "SUCCEEDED" if feedback_statuses == {"SUCCEEDED"}
            else "FAILED" if "FAILED" in feedback_statuses else "PARTIAL"
        )
        return {
            "schema_version": CLOSED_LOOP_CYCLE_SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "status": cycle_status,
            "state_before": state_before,
            "stage_results": deepcopy(context["stage_results"]),
            "execution_feedback": feedback_items,
            "next_state": self.state_manager.get_world_state(),
            "revision_before": revision_before,
            "revision_after": self.state_manager.state_revision,
            "trace": trace,
        }
