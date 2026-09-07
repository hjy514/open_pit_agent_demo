#!/usr/bin/env bash
# One-command P4 validation entry point.  CARLA itself must already be running.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${OPENPIT_PYTHON:-}" && -x "${OPENPIT_PYTHON}" ]]; then
  python_bin="${OPENPIT_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/openpit-agent/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/openpit-agent/bin/python"
else
  echo "ERROR: 未找到 openpit-agent Python；请设置 OPENPIT_PYTHON。" >&2
  exit 1
fi

if [[ -z "${OPENPIT_CARLA_ROOT:-}" ]]; then
  for candidate_root in \
    "${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor" \
    "${HOME}/carla"; do
    if [[ -d "${candidate_root}/PythonAPI/carla/dist" ]]; then
      export OPENPIT_CARLA_ROOT="${candidate_root}"
      break
    fi
  done
fi
if [[ -z "${OPENPIT_CARLA_ROOT:-}" ]]; then
  echo "ERROR: 未找到 CARLA 根目录；请设置 OPENPIT_CARLA_ROOT。" >&2
  exit 1
fi

cd "${project_dir}"
echo "[P4] Python：${python_bin}"
echo "[P4] CARLA目录：${OPENPIT_CARLA_ROOT}"
"${python_bin}" scripts/check_phase4_preflight.py "$@"
"${python_bin}" scripts/validate_dual_spawn_pairs.py "$@"
