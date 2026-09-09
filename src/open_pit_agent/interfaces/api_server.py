"""
OpenPit Agent API Server V4。

API服务持有唯一RuntimeState。run_demo.py通过/runtime/sync推送状态，
UI通过/commands写入控制命令，CARLA运行进程读取并回执。
"""

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from open_pit_agent.control import ControlError, UnifiedScenarioControlManager
from open_pit_agent.runtime_state import runtime


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CAMERA_ROOT = PROJECT_ROOT / "artifacts" / "live_cameras"
scenario_control = UnifiedScenarioControlManager(PROJECT_ROOT)


app = FastAPI(
    title="OpenPit Agent API",
    version="6.0-unified-scenario-control",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "OpenPit Agent API",
        "status": "running",
        "runtime": True,
        "version": "6.0-unified-scenario-control",
        "monitoring_endpoint": "/monitoring",
        "camera_endpoint": "/camera/streams",
        "scenario_catalog_endpoint": "/scenario/catalog",
        "scenario_control_endpoint": "/scenario/control/status",
    }


@app.get("/scenario/catalog")
def scenario_catalog():
    return {
        "schema_version": "openpit.scenario-control-catalog.v1",
        "scenarios": scenario_control.scenarios(),
    }


@app.get("/scenario/control/status")
def scenario_control_status():
    status = scenario_control.status()
    request = status.get("request") or {}
    requested_scenario = str(request.get("scenario_id") or "")
    runtime_scenario = dict(runtime.scenario or {})
    runtime_key = str(
        runtime_scenario.get("scenario_key")
        or runtime_scenario.get("scenario_id")
        or ""
    ).lower()
    runtime_matches = bool(
        requested_scenario
        and (
            runtime_key == requested_scenario
            or runtime_key.startswith(requested_scenario + "-")
        )
    )
    execution = dict(runtime.execution or {}) if runtime_matches else {}
    pending = (
        runtime.get_pending_decision_points() if runtime_matches else []
    )
    state = str(status.get("state") or "idle")
    if status.get("running"):
        if pending:
            state = "waiting_confirmation"
        elif not runtime_matches:
            state = (
                "loading_map"
                if request.get("mode") == "carla"
                else "starting_scenario"
            )
        elif str(execution.get("phase") or "") == "await_human_review":
            state = "waiting_confirmation"
    result = status.get("result") or {}
    runtime_tasks = list(runtime.tasks) if runtime_matches else []
    runtime_completed = sum(
        str(item.get("status") or "").lower() == "completed"
        for item in runtime_tasks if isinstance(item, dict)
    )
    status.update({
        "state": state,
        "scenario_id": requested_scenario or None,
        "seed": request.get("seed"),
        "vehicle_count": request.get("vehicle_count"),
        "run_id": result.get("run_id") or (
            runtime.run_id if runtime_matches else None
        ),
        "current_phase": execution.get("phase"),
        "completed_task_count": (
            result.get("completed_task_count")
            if result.get("completed_task_count") is not None
            else runtime_completed if runtime_matches else None
        ),
        "task_count": (
            result.get("task_count")
            if result.get("task_count") is not None
            else len(runtime_tasks) if runtime_matches else None
        ),
        "pending_decision_count": len(pending),
        "database_path": result.get("database_path"),
        "error": status.get("error") or (
            status.get("carla") or {}
        ).get("error"),
    })
    return status


@app.post("/scenario/control/start")
def scenario_control_start(payload: dict):
    try:
        return scenario_control.start(payload)
    except ControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/scenario/control/pause")
def scenario_control_pause():
    try:
        return scenario_control.pause()
    except ControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/scenario/control/resume")
def scenario_control_resume():
    try:
        return scenario_control.resume()
    except ControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/scenario/control/terminate")
def scenario_control_terminate():
    try:
        return scenario_control.terminate()
    except ControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/scenario/control/stop")
def scenario_control_stop_compatibility():
    """Compatibility endpoint; new UI uses the explicit terminate action."""
    return scenario_control_terminate()


@app.on_event("shutdown")
def close_scenario_control():
    scenario_control.close()


@app.get("/vehicles")
def vehicles():
    return runtime.vehicles


@app.get("/agents")
def agents():
    return runtime.get_agents()


@app.get("/dispatch")
def dispatch():
    return runtime.get_dispatch()


@app.get("/events")
def events():
    return runtime.events


@app.get("/monitoring")
def monitoring():
    return runtime.get_monitoring()


@app.get("/state")
def state():
    return runtime.get_state()


@app.get("/map_state")
def map_state():
    return runtime.get_map_state()


@app.get("/camera/streams")
def camera_streams():
    manifest_path = CAMERA_ROOT / "manifest.json"
    if not manifest_path.exists():
        return {"status": "offline", "streams": []}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="camera_manifest_unavailable: {}".format(exc),
        )
    streams = payload.get("streams", [])
    newest_mtime = 0.0
    for stream in streams:
        stream_id = str(stream.get("id", ""))
        frame_path = CAMERA_ROOT / "{}.png".format(stream_id)
        available = frame_path.exists()
        stream["frame_available"] = available
        if available:
            try:
                newest_mtime = max(
                    newest_mtime, frame_path.stat().st_mtime
                )
            except FileNotFoundError:
                stream["frame_available"] = False
    payload["status"] = (
        "online"
        if newest_mtime and time.time() - newest_mtime < 3.0
        else "stale"
    )
    return payload


@app.get("/camera/{stream_id}/frame")
def camera_frame(stream_id: str):
    if not stream_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in stream_id.lower()
    ):
        raise HTTPException(status_code=400, detail="invalid_camera_id")
    frame_path = CAMERA_ROOT / "{}.png".format(stream_id)
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="camera_frame_not_ready")
    return FileResponse(
        str(frame_path),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/runtime/sync")
def runtime_sync(payload: dict):
    runtime.sync_snapshot(payload)
    return {
        "status": "synced",
        "run_id": runtime.run_id,
        "vehicle_count": len(runtime.vehicles),
        "task_count": len(runtime.tasks),
        "event_count": len(runtime.events),
        "state_revision": runtime.state_revision,
        "execution_feedback_count": len(runtime.execution_feedback_history),
    }


@app.post("/runtime/reset")
def runtime_reset():
    runtime.reset()
    return {"status": "reset"}


@app.get("/decision-points/pending")
def pending_decision_points():
    """Return only decision points that genuinely require operator action."""
    return runtime.get_pending_decision_points()


@app.get("/decision-points/{decision_point_id}")
def get_decision_point(decision_point_id: str):
    item = runtime.get_decision_point(decision_point_id)
    if item is None:
        raise HTTPException(status_code=404, detail="decision_point_not_found")
    return item


@app.post("/decision-points/{decision_point_id}/resolve")
def resolve_decision_point(decision_point_id: str, payload: dict):
    try:
        item = runtime.resolve_decision_point(
            decision_point_id,
            payload.get("response"),
            source=payload.get("source", "human_operator"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="decision_point_not_found")
    return {"status": "resolved", "decision_point": item}


@app.post("/commands")
def create_command(command: dict):
    try:
        created = runtime.add_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "queued",
        "command": created,
    }


@app.get("/commands")
def commands(limit: int = Query(100, ge=1, le=500)):
    return runtime.get_commands(limit=limit)


@app.get("/commands/pending")
def pending_commands(limit: int = Query(20, ge=1, le=100)):
    return runtime.get_pending_commands(limit=limit)


@app.post("/commands/{command_id}/ack")
def acknowledge_command(command_id: str, payload: dict):
    status = str(payload.get("status", "")).strip()
    try:
        updated = runtime.acknowledge_command(
            command_id,
            status=status,
            message=payload.get("message"),
            result=payload.get("result"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if updated is None:
        raise HTTPException(status_code=404, detail="command_not_found")

    return {
        "status": "acknowledged",
        "command": updated,
    }


@app.post("/manual_dispatch")
def manual_dispatch(command: dict):
    """兼容旧UI；物理控制类操作统一进入命令队列。"""

    print("receive manual command:", command)
    action = command.get("action")

    if action in {
        "emergency_stop",
        "pause_vehicle",
        "resume_vehicle",
        "manual_dispatch",
    }:
        try:
            queued = runtime.add_command(command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "status": "queued",
            "command": queued,
        }

    if action == "approve_ai_plan":
        runtime.add_event(
            "Human Operator",
            "调度员接受AI方案：{}".format(
                command.get("task_id", "-")
            ),
        )
        runtime.decision = {
            "status": "APPROVED_BY_HUMAN",
            "task_id": command.get("task_id"),
            "vehicle_id": command.get("vehicle_id"),
        }
        return {
            "status": "executed",
            "command": command,
        }

    if action == "reject_ai_plan":
        runtime.add_event(
            "Human Operator",
            "调度员驳回AI方案",
        )
        runtime.decision = {
            "status": "REJECTED_BY_HUMAN",
        }
        return {
            "status": "executed",
            "command": command,
        }

    runtime.add_event("Human Operator", str(command))
    return {
        "status": "received",
        "command": command,
    }
