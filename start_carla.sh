#!/usr/bin/env bash
set -euo pipefail

# Local convenience launcher.  OPENPIT_CARLA_ROOT remains an optional
# cross-computer override; regular use needs no export command.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_CARLA_ROOT:-}" && -x "${OPENPIT_CARLA_ROOT}/CarlaUE4.sh" ]]; then
  carla_root="${OPENPIT_CARLA_ROOT}"
elif [[ -x "${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor/CarlaUE4.sh" ]]; then
  carla_root="${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor"
elif [[ -x "${HOME}/carla/CarlaUE4.sh" ]]; then
  carla_root="${HOME}/carla"
else
  echo "ERROR: 未找到 CARLA；请设置 OPENPIT_CARLA_ROOT。" >&2
  exit 1
fi

cd "${carla_root}"
echo "[OpenPit CARLA] 目录：${carla_root}"
echo "[OpenPit CARLA] 启动参数：${*:-无（使用CARLA默认画质）}"
exec ./CarlaUE4.sh "$@"
