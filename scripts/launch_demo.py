#!/usr/bin/env python3
"""One-command lifecycle for CARLA and the desktop dashboard window."""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from launch_desktop import find_browser
from open_pit_agent.adapters.carla_adapter import (
    CarlaAdapter,
    CarlaAdapterError,
)
from open_pit_agent.config import load_config


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "town03.json"
DESKTOP_LAUNCHER = PROJECT_ROOT / "scripts" / "launch_desktop.py"


class DemoLaunchError(RuntimeError):
    """Raised when the combined CARLA/Desktop lifecycle fails."""


def get_config_path(args):
    if args.config:
        return PROJECT_ROOT / args.config
    return DEFAULT_CONFIG


def carla_command(carla_root):
    launcher = Path(carla_root) / "CarlaUE4.sh"
    if not launcher.is_file():
        raise DemoLaunchError(
            "CARLA launcher not found: {}".format(launcher)
        )
    # The custom mine map relies on CARLA's default rendering settings.
    # Do not force low-quality flags here: they can leave map resources blank.
    return [str(launcher)]


def desktop_command(python_executable, fullscreen=False):
    command = [
        python_executable,
        str(DESKTOP_LAUNCHER),
    ]
    if fullscreen:
        command.append("--fullscreen")
    return command


def probe_carla(config_path, timeout_seconds=2.0):
    config = load_config(config_path)
    adapter = CarlaAdapter(config)
    adapter._import_carla()
    try:
        client = adapter.carla.Client(
            config.carla.host, config.carla.port
        )
        client.set_timeout(timeout_seconds)
        world = client.get_world()
        map_name = world.get_map().name.split("/")[-1]
        return {
            "ready": True,
            "host": config.carla.host,
            "port": config.carla.port,
            "map_name": map_name,
        }
    except RuntimeError as exc:
        return {
            "ready": False,
            "host": config.carla.host,
            "port": config.carla.port,
            "error": str(exc),
        }


def wait_for_carla(config_path, process, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DemoLaunchError(
                "CARLA exited before becoming ready "
                "(code {})".format(process.returncode)
            )
        last_status = probe_carla(config_path)
        if last_status["ready"]:
            return last_status
        time.sleep(1.0)
    raise DemoLaunchError(
        "CARLA did not become ready within {} seconds: {}".format(
            timeout_seconds, last_status
        )
    )


def stop_owned_carla(process, process_group_id):
    if process is None or process_group_id is None:
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=12.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Check/start CARLA and open the standalone demonstration window"
        )
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Scenario config path relative to project root",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Open the dashboard in presentation fullscreen",
    )
    parser.add_argument(
        "--keep-carla",
        action="store_true",
        help="Keep CARLA running after the desktop window closes",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Report Chrome and CARLA readiness without starting applications",
    )
    parser.add_argument(
        "--carla-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for a newly started CARLA",
    )
    args = parser.parse_args()

    config_path = get_config_path(args)

    browser_path = find_browser()
    config = load_config(config_path)
    initial_status = probe_carla(config_path)

    if args.check_only:
        print("桌面窗口引擎：{}".format(browser_path))
        if initial_status["ready"]:
            print(
                "CARLA已就绪：{}:{}，地图={}".format(
                    initial_status["host"],
                    initial_status["port"],
                    initial_status["map_name"],
                )
            )
        else:
            print(
                "CARLA未就绪：{}:{}，运行一键启动时将自动启动".format(
                    initial_status["host"],
                    initial_status["port"],
                )
            )
        return

    carla_process = None
    carla_process_group_id = None
    carla_log_handle = None
    carla_started_here = False

    try:
        if initial_status["ready"]:
            print(
                "检测到现有CARLA，正在复用（地图={}）".format(
                    initial_status["map_name"]
                )
            )
        else:
            log_root = PROJECT_ROOT / "artifacts" / "control"
            log_root.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime(
                "%Y%m%dT%H%M%S", time.gmtime()
            )
            log_path = log_root / (
                "{}-carla-runtime.log".format(timestamp)
            )
            carla_log_handle = log_path.open(
                "a", encoding="utf-8"
            )

            command = carla_command(config.carla.root)

            print("CARLA未运行，正在启动……")
            print("CARLA日志：{}".format(log_path))

            carla_process = subprocess.Popen(
                command,
                cwd=str(config.carla.root),
                stdin=subprocess.DEVNULL,
                stdout=carla_log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            carla_process_group_id = carla_process.pid
            carla_started_here = True

            status = wait_for_carla(
                config_path,
                carla_process,
                args.carla_timeout,
            )

            print(
                "CARLA已就绪：{}:{}，地图={}".format(
                    status["host"],
                    status["port"],
                    status["map_name"],
                )
            )

        desktop_process = subprocess.Popen(
            desktop_command(
                sys.executable,
                fullscreen=args.fullscreen,
            ),
            cwd=str(PROJECT_ROOT),
        )

        exit_code = desktop_process.wait()

        if exit_code != 0:
            raise DemoLaunchError(
                "Desktop window exited with code {}".format(exit_code)
            )

    finally:
        if carla_started_here and not args.keep_carla:
            print("正在停止本次启动的CARLA……")
            stop_owned_carla(
                carla_process,
                carla_process_group_id,
            )

        if carla_log_handle is not None:
            carla_log_handle.close()


if __name__ == "__main__":
    try:
        main()
    except (
        CarlaAdapterError,
        DemoLaunchError,
        OSError,
    ) as exc:
        raise SystemExit("ERROR: {}".format(exc))
