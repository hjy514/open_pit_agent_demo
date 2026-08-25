#!/usr/bin/env python3
"""Read-only checks for the configured CARLA/Python environment."""

import glob
import platform
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.config import ConfigError, load_config


def main() -> int:
    config_path = PROJECT_ROOT / "configs" / "town03.json"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print("[FAIL] 配置：{}".format(exc))
        return 1

    print("[INFO] Python: {}".format(platform.python_version()))
    if sys.version_info[:2] == (3, 7):
        print("[PASS] Python版本与CARLA 0.9.10 egg匹配")
    else:
        print("[WARN] 建议使用tcp37环境；当前解释器不是Python 3.7")

    carla_root = config.carla.root.expanduser().resolve()
    launcher = carla_root / "CarlaUE4.sh"
    version_file = carla_root / "VERSION"
    print(
        "[{}] CARLA启动器：{}".format(
            "PASS" if launcher.is_file() else "FAIL", launcher
        )
    )
    if version_file.is_file():
        print("[PASS] CARLA版本：{}".format(version_file.read_text().strip()))
    else:
        print("[FAIL] 未找到CARLA VERSION文件")

    eggs = sorted(
        glob.glob(
            str(
                carla_root
                / "PythonAPI"
                / "carla"
                / "dist"
                / "carla-*-py3.7-linux-x86_64.egg"
            )
        )
    )
    if not eggs:
        print("[FAIL] 未找到Python 3.7 CARLA egg")
        return 1
    print("[PASS] CARLA egg：{}".format(eggs[-1]))
    sys.path.insert(0, eggs[-1])
    try:
        import carla
    except ImportError as exc:
        print("[FAIL] import carla：{}".format(exc))
        return 1
    print("[PASS] import carla：{}".format(carla.__file__))
    print("[INFO] 本脚本不会启动或连接CARLA服务")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

