"""
OpenPit Agent API Server V4。

API服务持有唯一RuntimeState。run_demo.py通过/runtime/sync推送状态，
UI通过/commands写入控制命令，CARLA运行进程读取并回执。
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from open_pit_agent.runtime_state import runtime


app = FastAPI(
    title="OpenPit Agent API",
    version="4.0",
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
        "version": "4.0",
    }


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


@app.get("/state")
def state():
    return runtime.get_state()


@app.get("/map_state")
def map_state():
    return runtime.get_map_state()


@app.post("/runtime/sync")
def runtime_sync(payload: dict):
    runtime.sync_snapshot(payload)
    return {
        "status": "synced",
        "run_id": runtime.run_id,
        "vehicle_count": len(runtime.vehicles),
        "task_count": len(runtime.tasks),
        "event_count": len(runtime.events),
    }


@app.post("/runtime/reset")
def runtime_reset():
    runtime.reset()
    return {"status": "reset"}


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
