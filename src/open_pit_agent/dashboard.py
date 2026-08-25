"""Read-only projection of run evidence for the local dashboard."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


TERMINAL_TASK_STATUSES = {
    "completed",
    "timed_out",
    "cancelled",
}


class DashboardDataError(RuntimeError):
    """Raised when dashboard evidence cannot be read safely."""


class RunRepository:
    def __init__(self, artifacts_root: Path) -> None:
        self.artifacts_root = Path(artifacts_root).expanduser().resolve()

    def list_runs(self) -> List[Dict[str, Any]]:
        if not self.artifacts_root.exists():
            return []
        result = []
        for run_dir in sorted(
            (
                item
                for item in self.artifacts_root.iterdir()
                if item.is_dir()
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            summary = _read_json_if_present(
                run_dir / "summary.json"
            )
            result.append(
                {
                    "run_id": run_dir.name,
                    "scenario_id": (
                        summary.get("scenario_id")
                        if summary
                        else None
                    ),
                    "status": (
                        summary.get("status")
                        if summary
                        else "RUNNING"
                    ),
                    "completed": summary is not None,
                    "modified_at": run_dir.stat().st_mtime,
                }
            )
        return result

    def resolve_run(
        self, run_id: Optional[str] = None
    ) -> Path:
        runs = self.list_runs()
        if not runs:
            raise DashboardDataError(
                "No run evidence found under {}".format(
                    self.artifacts_root
                )
            )
        selected_id = run_id or str(runs[0]["run_id"])
        known_ids = {
            str(item["run_id"]) for item in runs
        }
        if selected_id not in known_ids:
            raise DashboardDataError(
                "Unknown run_id: {}".format(selected_id)
            )
        return self.artifacts_root / selected_id

    def state(
        self, run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        run_dir = self.resolve_run(run_id)
        events = read_events(run_dir / "events.jsonl")
        summary = _read_json_if_present(
            run_dir / "summary.json"
        )
        return project_run_state(
            run_dir.name, events, summary
        )

    def events(
        self,
        run_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        run_dir = self.resolve_run(run_id)
        events = read_events(run_dir / "events.jsonl")
        bounded_limit = max(1, min(int(limit), 500))
        return events[-bounded_limit:]


def read_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    # A live writer may have a final incomplete line.
                    continue
    except OSError as exc:
        raise DashboardDataError(
            "Unable to read {}: {}".format(path, exc)
        ) from exc
    return events


def project_run_state(
    run_id: str,
    events: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    vehicles: Dict[str, Dict[str, Any]] = {}
    tasks: Dict[str, Dict[str, Any]] = {}
    work_orders: Dict[str, Dict[str, Any]] = {}
    restrictions: Dict[str, Dict[str, Any]] = {}
    assessments: List[Dict[str, Any]] = []
    guidance_by_assessment: Dict[str, Dict[str, Any]] = {}
    zones: Dict[str, Dict[str, Any]] = {}
    vehicle_trails: Dict[str, List[Dict[str, float]]] = {}
    latest_tick = 0
    scenario_id = None

    for event in events:
        event_type = str(event.get("event_type", "unknown"))
        payload = event.get("payload") or {}
        event_tick = payload.get("tick")
        if isinstance(event_tick, int):
            latest_tick = max(latest_tick, event_tick)

        if event_type in {
            "carla_connected",
            "initial_vehicle_states",
        }:
            _replace_vehicles(
                vehicles, payload.get("vehicles", [])
            )
        elif event_type == "carla_tick":
            _replace_vehicles(
                vehicles, payload.get("vehicles", [])
            )
            _append_vehicle_trails(
                vehicle_trails,
                payload.get("vehicles", []),
                event_tick,
            )
        elif event_type == "runtime_scene":
            for zone in payload.get("zones", []):
                zone_id = zone.get("zone_id")
                if zone_id:
                    zones[str(zone_id)] = dict(zone)
        elif event_type == "initial_schedule":
            for assignment in payload.get(
                "assignments", []
            ):
                task_id = str(assignment["task_id"])
                tasks[task_id] = {
                    "task_id": task_id,
                    "zone_id": assignment.get("zone_id"),
                    "assigned_vehicle_id": assignment.get(
                        "vehicle_id"
                    ),
                    "task_type": "routine_inspection",
                    "status": "assigned",
                    "priority": None,
                }
            for task in payload.get("tasks", []):
                tasks[str(task["task_id"])] = dict(task)
        elif event_type == "risk_assessed":
            assessments.append(dict(payload))
        elif event_type == "risk_guidance_generated":
            assessment_id = payload.get("assessment_id")
            if assessment_id:
                guidance_by_assessment[str(assessment_id)] = dict(
                    payload
                )
        elif event_type == "risk_task_created":
            task = payload.get("task", {})
            if task.get("task_id"):
                tasks[str(task["task_id"])] = dict(task)
        elif event_type == "work_order_created":
            order_id = payload.get("work_order_id")
            if order_id:
                work_orders[str(order_id)] = dict(payload)
        elif event_type == "work_order_status_changed":
            order_id = payload.get("work_order_id")
            if order_id:
                order = work_orders.setdefault(
                    str(order_id),
                    {
                        "work_order_id": order_id,
                        "task_id": payload.get("task_id"),
                    },
                )
                order["status"] = payload.get("to_status")
                order["updated_tick"] = payload.get("tick")
        elif event_type == "road_restriction_activated":
            restriction = payload.get("restriction", {})
            restriction_id = restriction.get(
                "restriction_id"
            )
            if restriction_id:
                restrictions[str(restriction_id)] = dict(
                    restriction
                )
            for task_id in payload.get(
                "cancelled_task_ids", []
            ):
                _update_task_status(
                    tasks, str(task_id), "cancelled"
                )
        elif event_type == "task_started":
            _update_task_status(
                tasks,
                str(payload.get("task_id")),
                "executing",
                payload.get("vehicle_id"),
            )
            task_id = payload.get("task_id")
            if task_id and payload.get(
                "target_position"
            ):
                tasks.setdefault(
                    str(task_id),
                    {"task_id": str(task_id)},
                )["target_position"] = payload[
                    "target_position"
                ]
        elif event_type == "task_completed":
            _update_task_status(
                tasks,
                str(payload.get("task_id")),
                "completed",
                payload.get("vehicle_id"),
            )
        elif event_type == "task_timed_out":
            _update_task_status(
                tasks,
                str(payload.get("task_id")),
                "timed_out",
                payload.get("vehicle_id"),
            )
        elif event_type == "task_preempted":
            _update_task_status(
                tasks,
                str(payload.get("preempted_task_id")),
                "assigned",
                payload.get("vehicle_id"),
            )
        elif event_type == "run_completed":
            scenario_id = payload.get("scenario_id")

    if summary:
        scenario_id = summary.get(
            "scenario_id", scenario_id
        )
        _replace_vehicles(
            vehicles, summary.get("vehicle_states", [])
        )
        tasks = {
            str(item["task_id"]): dict(item)
            for item in summary.get("tasks", [])
        }
        work_orders = {
            str(item["work_order_id"]): dict(item)
            for item in summary.get("work_orders", [])
        }
        restrictions = {
            str(item["restriction_id"]): dict(item)
            for item in summary.get(
                "road_restrictions", []
            )
        }
        assessments = list(
            summary.get("risk_assessments", assessments)
        )
        guidance_by_assessment = {
            str(item["assessment_id"]): dict(item)
            for item in summary.get("risk_guidance", [])
            if item.get("assessment_id")
        }
        zones = {
            str(item["zone_id"]): dict(item)
            for item in summary.get(
                "zones", zones.values()
            )
        }
        latest_tick = int(
            summary.get("ticks_executed", latest_tick)
        )

    task_values = list(tasks.values())
    completed_tasks = sum(
        item.get("status") == "completed"
        for item in task_values
    )
    work_order_values = list(work_orders.values())
    closed_orders = sum(
        item.get("status") == "closed"
        for item in work_order_values
    )
    enriched_assessments = []
    for assessment in assessments:
        enriched = dict(assessment)
        guidance = guidance_by_assessment.get(
            str(assessment.get("assessment_id"))
        )
        if guidance:
            enriched["guidance"] = guidance
        enriched_assessments.append(enriched)
    state = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "phase": "completed" if summary else "running",
        "status": (
            summary.get("status", "RUNNING")
            if summary
            else "RUNNING"
        ),
        "latest_tick": latest_tick,
        "vehicles": sorted(
            vehicles.values(),
            key=lambda item: str(
                item.get("vehicle_id", "")
            ),
        ),
        "zones": sorted(
            zones.values(),
            key=lambda item: str(
                item.get("zone_id", "")
            ),
        ),
        "vehicle_trails": vehicle_trails,
        "tasks": sorted(
            task_values,
            key=lambda item: str(item.get("task_id", "")),
        ),
        "risk_assessments": enriched_assessments,
        "current_risk": (
            enriched_assessments[-1]
            if enriched_assessments
            else None
        ),
        "road_restrictions": sorted(
            restrictions.values(),
            key=lambda item: str(
                item.get("restriction_id", "")
            ),
        ),
        "work_orders": sorted(
            work_order_values,
            key=lambda item: str(
                item.get("work_order_id", "")
            ),
        ),
        "metrics": {
            "task_count": len(task_values),
            "completed_task_count": completed_tasks,
            "task_completion_rate": (
                round(
                    completed_tasks
                    / float(len(task_values)),
                    4,
                )
                if task_values
                else 0.0
            ),
            "work_order_count": len(
                work_order_values
            ),
            "closed_work_order_count": closed_orders,
            "active_restriction_count": sum(
                item.get("status") == "active"
                for item in restrictions.values()
            ),
        },
        "capability_boundary": {
            "synthetic_risk_data": bool(
                summary
                and summary.get("risk_dataset_label")
            )
            or bool(assessments),
            "route_avoidance_enforced": bool(
                summary
                and summary.get(
                    "route_avoidance_enforced", False
                )
            ),
        },
        "timeline": [
            compact_event(item)
            for item in events[-80:]
        ],
    }
    return state


def compact_event(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload") or {}
    event_type = str(event.get("event_type", "unknown"))
    result = {
        "event_type": event_type,
        "timestamp": event.get("timestamp"),
        "tick": payload.get("tick"),
    }
    for key in (
        "vehicle_id",
        "task_id",
        "work_order_id",
        "to_status",
        "risk_level",
        "level",
    ):
        if key in payload:
            result[key] = payload[key]
    if event_type == "risk_assessed":
        result["level"] = payload.get("level")
        result["zone_id"] = payload.get("zone_id")
    elif event_type == "road_restriction_activated":
        restriction = payload.get("restriction", {})
        result["restriction_id"] = restriction.get(
            "restriction_id"
        )
        result["road_segment_id"] = restriction.get(
            "road_segment_id"
        )
    elif event_type == "run_completed":
        result["status"] = payload.get("status")
    return result


def _replace_vehicles(
    target: Dict[str, Dict[str, Any]],
    values: List[Dict[str, Any]],
) -> None:
    for vehicle in values:
        vehicle_id = vehicle.get("vehicle_id")
        if vehicle_id:
            target[str(vehicle_id)] = dict(vehicle)


def _append_vehicle_trails(
    target: Dict[str, List[Dict[str, float]]],
    vehicles: List[Dict[str, Any]],
    tick: Optional[int],
) -> None:
    for vehicle in vehicles:
        vehicle_id = vehicle.get("vehicle_id")
        position = vehicle.get("position")
        if not vehicle_id or not position:
            continue
        points = target.setdefault(str(vehicle_id), [])
        points.append(
            {
                "x": float(position.get("x", 0.0)),
                "y": float(position.get("y", 0.0)),
                "tick": int(tick or 0),
            }
        )
        if len(points) > 240:
            del points[:-240]


def _update_task_status(
    tasks: Dict[str, Dict[str, Any]],
    task_id: str,
    status: str,
    vehicle_id: Optional[str] = None,
) -> None:
    if not task_id or task_id == "None":
        return
    task = tasks.setdefault(
        task_id,
        {
            "task_id": task_id,
            "task_type": "unknown",
            "zone_id": None,
            "priority": None,
        },
    )
    task["status"] = status
    if vehicle_id:
        task["assigned_vehicle_id"] = vehicle_id


def _read_json_if_present(
    path: Path,
) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise DashboardDataError(
            "Unable to read {}: {}".format(path, exc)
        ) from exc
