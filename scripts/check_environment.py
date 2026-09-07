#!/usr/bin/env python3
"""Read-only preflight checks for the OpenPit CARLA demonstration."""

from __future__ import print_function

import argparse
import glob
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib import request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter, CarlaAdapterError
from open_pit_agent.config import ConfigError, load_config


def _result(ok, label, detail):
    print("[{}] {}：{}".format("PASS" if ok else "FAIL", label, detail))
    return bool(ok)


def _warn(label, detail):
    print("[WARN] {}：{}".format(label, detail))


def _parse_args():
    parser = argparse.ArgumentParser(
        description="只读检查 OpenPit Demo 的 Python、CARLA、地图、数据库与可选服务。"
    )
    parser.add_argument(
        "--config", default="configs/mine_competition_demo.json",
        help="场景配置路径（默认：当前边坡 Demo 配置）",
    )
    parser.add_argument("--check-api", action="store_true", help="额外检查 Agent API 根接口。")
    parser.add_argument(
        "--api-url", default=os.environ.get("OPENPIT_API_URL", "http://127.0.0.1:8000"),
        help="Agent API 地址。",
    )
    parser.add_argument(
        "--dispatch-python",
        help="额外使用指定解释器检查 PyQt6，例如 $OPENPIT_DISPATCH_PYTHON。",
    )
    parser.add_argument(
        "--check-ui", action="store_true",
        help="自动查找 openpit-ui 环境并检查 PyQt6。",
    )
    parser.add_argument(
        "--require-target-map", action="store_true",
        help="将当前不是配置目标地图视为失败；适用于地图已加载后的复核。",
    )
    return parser.parse_args()


def _check_database(path, label, required_tables):
    if not path.is_file():
        return _result(False, label, "未找到 {}".format(path))
    try:
        connection = sqlite3.connect(str(path))
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        connection.close()
    except sqlite3.Error as exc:
        return _result(False, label, "无法读取：{}".format(exc))
    names = {row[0] for row in rows}
    missing = sorted(set(required_tables).difference(names))
    if missing:
        return _result(False, label, "缺少表：{}".format(", ".join(missing)))
    return _result(True, label, "可读取，表数量={}".format(len(names)))


def _check_dispatch_python(python_bin):
    candidate = Path(python_bin).expanduser()
    if not candidate.is_file() or not os.access(str(candidate), os.X_OK):
        return _result(False, "调度中心 Python", "不可执行：{}".format(candidate))
    completed = subprocess.run(
        [str(candidate), "-c", "import PyQt6; print(PyQt6.__file__)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    if completed.returncode:
        return _result(False, "调度中心 PyQt6", completed.stderr.strip() or "import PyQt6 失败")
    return _result(True, "调度中心 PyQt6", completed.stdout.strip())


def main():
    args = _parse_args()
    config_path = (PROJECT_ROOT / args.config).resolve()
    passed = True

    print("OpenPit Demo 启动前健康检查（只读）")
    print("[INFO] 配置：{}".format(config_path))
    print("[INFO] Python：{} ({})".format(platform.python_version(), sys.executable))
    if sys.version_info[:2] != (3, 7):
        _warn("Python版本", "CARLA 0.9.10 本项目建议使用 Python 3.7 环境")
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _result(False, "场景配置", str(exc))
        return 1
    _result(True, "场景配置", "{}；目标地图={}".format(config.scenario_id, config.carla.map_name))

    carla_root = config.carla.root.expanduser().resolve()
    launcher = carla_root / "CarlaUE4.sh"
    passed &= _result(launcher.is_file(), "CARLA启动器", str(launcher))
    egg_pattern = str(carla_root / "PythonAPI" / "carla" / "dist" / "carla-*-py3.7-linux-x86_64.egg")
    eggs = sorted(glob.glob(egg_pattern))
    passed &= _result(bool(eggs), "CARLA Python API", eggs[-1] if eggs else "未找到 Python 3.7 egg")

    if eggs:
        try:
            adapter = CarlaAdapter(config)
            adapter._import_carla()
            client = adapter.carla.Client(config.carla.host, config.carla.port)
            client.set_timeout(min(config.carla.timeout_seconds, 10.0))
            server_version = client.get_server_version()
            carla_map = client.get_world().get_map()
            current_name = carla_map.name.split("/")[-1]
            if current_name == config.carla.map_name:
                _result(True, "CARLA当前地图", "{}".format(current_name))
            elif args.require_target_map:
                passed &= _result(
                    False, "CARLA当前地图",
                    "{}（期望 {}）".format(current_name, config.carla.map_name),
                )
            else:
                _warn(
                    "CARLA当前地图",
                    "{}；正式场景启动时会加载 {}".format(
                        current_name, config.carla.map_name
                    ),
                )
            spawn_count = len(carla_map.get_spawn_points())
            topology_count = len(carla_map.get_topology())
            passed &= _result(
                spawn_count > 0 and topology_count > 0, "地图路网",
                "server={}；出生点={}；拓扑边={}".format(server_version, spawn_count, topology_count),
            )
            blueprint = client.get_world().get_blueprint_library().find("vehicle.cat.cat")
            passed &= _result(blueprint is not None, "矿卡蓝图", "vehicle.cat.cat")
        except (CarlaAdapterError, RuntimeError) as exc:
            passed &= _result(False, "CARLA服务/地图路网", str(exc))

    passed &= _check_database(
        PROJECT_ROOT / "data" / "database" / "openpit.db",
        "运行数据库 openpit.db", ("scenario_runs", "events", "decisions", "tasks"),
    )
    passed &= _check_database(
        PROJECT_ROOT / "data" / "database" / "map_resources.db",
        "地图资源库 map_resources.db", ("map_points", "calibration_runs", "point_conflicts"),
    )

    if args.check_api:
        try:
            with request.urlopen(args.api_url.rstrip("/") + "/", timeout=3.0) as response:
                payload = response.read().decode("utf-8")
            passed &= _result(True, "Agent API", "{} 返回 {}".format(args.api_url, payload[:120]))
        except Exception as exc:
            passed &= _result(False, "Agent API", "{} ({})".format(args.api_url, exc))
    else:
        print("[INFO] Agent API：未检查；如已启动请加 --check-api")

    dispatch_python = args.dispatch_python
    if args.check_ui and not dispatch_python:
        for candidate in (
            PROJECT_ROOT / "open_pit_dispatch_app" / ".venv" / "bin" / "python",
            Path.home() / "miniconda3" / "envs" / "openpit-ui" / "bin" / "python",
        ):
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                dispatch_python = str(candidate)
                break
    if dispatch_python:
        passed &= _check_dispatch_python(dispatch_python)
    else:
        print("[INFO] 调度中心 PyQt6：未检查；如需检查请加 --check-ui")
    print("\n{}".format("健康检查通过，可启动场景。" if passed else "健康检查失败，请先修复 FAIL 项。"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
