#!/usr/bin/env python3
"""Launch the dashboard as a standalone desktop-style application window."""

import argparse
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_SERVER = PROJECT_ROOT / "scripts" / "dashboard_server.py"
BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


class DesktopLaunchError(RuntimeError):
    """Raised when the local application window cannot be started."""


def find_browser(explicit_path=None):
    if explicit_path:
        resolved = shutil.which(explicit_path)
        if resolved:
            return resolved
        candidate = Path(explicit_path).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise DesktopLaunchError(
            "Browser executable not found: {}".format(
                explicit_path
            )
        )
    for candidate in BROWSER_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise DesktopLaunchError(
        "Google Chrome or Chromium is required for the "
        "standalone application window"
    )


def reserve_local_port():
    with socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    ) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def browser_command(
    browser_path, app_url, profile_dir, fullscreen=False
):
    command = [
        browser_path,
        "--app={}".format(app_url),
        "--user-data-dir={}".format(profile_dir),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--class=OpenPitAgentDemo",
        "--window-size=1500,950",
    ]
    if fullscreen:
        command.append("--start-fullscreen")
    return command


def wait_for_service(
    health_url, process, timeout_seconds=15.0
):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise DesktopLaunchError(
                "Visualization service exited with code "
                "{}".format(exit_code)
            )
        try:
            with urlopen(
                health_url, timeout=1.0
            ) as response:
                payload = json.loads(
                    response.read().decode("utf-8")
                )
                if payload.get("status") == "ok":
                    return
        except (OSError, URLError, ValueError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise DesktopLaunchError(
        "Visualization service did not become ready: "
        "{}".format(last_error)
    )


def stop_owned_process(process, timeout_seconds=8.0):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3.0)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Open the mining Agent dashboard in a standalone "
            "desktop-style window"
        )
    )
    parser.add_argument(
        "--browser",
        help="Chrome/Chromium executable name or path",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Open in presentation fullscreen mode",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "Verify the local service and browser dependency "
            "without opening a GUI window"
        ),
    )
    args = parser.parse_args()

    browser_path = find_browser(args.browser)
    port = reserve_local_port()
    app_url = "http://127.0.0.1:{}".format(port)
    profile_dir = Path(
        tempfile.mkdtemp(
            prefix="open_pit_agent_window_"
        )
    )
    log_root = PROJECT_ROOT / "artifacts" / "control"
    log_root.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime(
        "%Y%m%dT%H%M%S", time.gmtime()
    )
    log_path = log_root / (
        "{}-desktop-window.log".format(timestamp)
    )
    log_handle = None
    server_process = None
    browser_process = None
    try:
        log_handle = log_path.open(
            "a", encoding="utf-8"
        )
        server_process = subprocess.Popen(
            [
                sys.executable,
                str(DASHBOARD_SERVER),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        wait_for_service(
            "{}/api/health".format(app_url),
            server_process,
        )
        if args.check_only:
            print("桌面应用自检通过")
            print("浏览器：{}".format(browser_path))
            print("本地服务：{}".format(app_url))
            print("诊断日志：{}".format(log_path))
            return

        print("正在打开集群安全态势台窗口……")
        print("诊断日志：{}".format(log_path))
        browser_process = subprocess.Popen(
            browser_command(
                browser_path,
                app_url,
                profile_dir,
                fullscreen=args.fullscreen,
            ),
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            browser_process.wait()
        except KeyboardInterrupt:
            print("\n正在关闭集群安全态势台……")
    finally:
        if (
            browser_process is not None
            and browser_process.poll() is None
        ):
            browser_process.terminate()
        stop_owned_process(server_process)
        if log_handle is not None:
            log_handle.close()
        shutil.rmtree(
            str(profile_dir), ignore_errors=True
        )


if __name__ == "__main__":
    try:
        main()
    except DesktopLaunchError as exc:
        raise SystemExit("ERROR: {}".format(exc))
