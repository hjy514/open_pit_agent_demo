#!/usr/bin/env python3
"""Run one CARLA slope-hazard scenario and approve the takeover in time."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib import request


API_BASE = "http://127.0.0.1:8000"


def api_json(path, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        API_BASE + path, data=data, headers=headers, method=method
    )
    with request.urlopen(req, timeout=5.0) as response:
        return json.loads(response.read().decode("utf-8"))


def find_takeover_task(dispatch):
    for task in dispatch.get("tasks", []):
        if (
            task.get("handover_reason")
            and task.get("recommended_vehicle_id")
            and task.get("status") == "pending"
        ):
            return task
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", type=int, default=10000)
    parser.add_argument("--speed-limit-kmh", type=float, default=15.0)
    parser.add_argument("--approval-timeout", type=float, default=120.0)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    api_json("/runtime/reset", method="POST")
    command = [
        sys.executable,
        str(project_root / "run_demo.py"),
        "--mode",
        "carla-run",
        "--config",
        "configs/mine_competition_demo.json",
        "--risk-config",
        "configs/risk_slope_competition_synthetic.json",
        "--monitoring-config",
        "configs/monitoring_demo.json",
        "--load-map",
        "--spawn-missing",
        "--ticks",
        str(args.ticks),
    ]
    process = subprocess.Popen(command, cwd=str(project_root))
    deadline = time.time() + args.approval_timeout
    approved_task = None
    try:
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.5)
            try:
                dispatch = api_json("/dispatch")
            except Exception:
                continue
            task = find_takeover_task(dispatch)
            if task is None:
                continue
            vehicle_id = task["recommended_vehicle_id"]
            task_id = task["task_id"]
            api_json(
                "/manual_dispatch",
                method="POST",
                payload={
                    "action": "approve_ai_plan",
                    "run_id": dispatch.get("run_id"),
                    "task_id": task_id,
                    "vehicle_id": vehicle_id,
                    "source": "takeover_acceptance_test",
                },
            )
            queued = api_json(
                "/commands",
                method="POST",
                payload={
                    "action": "manual_dispatch",
                    "vehicle_id": vehicle_id,
                    "task_id": task_id,
                    "priority": "urgent",
                    "speed_limit_kmh": args.speed_limit_kmh,
                    "source": "human_approved_ai_takeover",
                },
            )
            approved_task = {
                "run_id": dispatch.get("run_id"),
                "task_id": task_id,
                "vehicle_id": vehicle_id,
                "command_id": queued["command"]["command_id"],
                "safe_merge_point": task.get("safe_merge_point"),
                "remaining_route_checkpoint_count": task.get(
                    "remaining_route_checkpoint_count"
                ),
            }
            print(
                "\n[Takeover Validation] 已下发人工确认接管：{}\n".format(
                    json.dumps(approved_task, ensure_ascii=False)
                ),
                flush=True,
            )
            break

        if approved_task is None:
            if process.poll() is not None:
                raise RuntimeError(
                    "CARLA场景在接管任务生成前退出：{}".format(
                        process.returncode
                    )
                )
            raise RuntimeError("超时：未发现待人工确认的接管任务")

        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError("CARLA场景运行失败：{}".format(return_code))

        commands = api_json("/commands")
        receipt = next(
            (
                item
                for item in commands
                if item.get("command_id") == approved_task["command_id"]
            ),
            None,
        )
        dispatch = api_json("/dispatch")
        task = next(
            (
                item
                for item in dispatch.get("tasks", [])
                if item.get("task_id") == approved_task["task_id"]
            ),
            None,
        )
        print(
            "[Takeover Validation] 执行回执：{}".format(
                json.dumps(receipt, ensure_ascii=False)
            )
        )
        print(
            "[Takeover Validation] 最终任务：{}".format(
                json.dumps(task, ensure_ascii=False)
            )
        )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10.0)


if __name__ == "__main__":
    main()
