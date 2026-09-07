#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_PYTHON:-}" && -x "${OPENPIT_PYTHON}" ]]; then
  python_bin="${OPENPIT_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/openpit-agent/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/openpit-agent/bin/python"
elif [[ -x "${HOME}/miniconda3/envs/tcp37/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/tcp37/bin/python"
else
  echo "ERROR: 未找到Agent Python；请设置 OPENPIT_PYTHON。" >&2
  exit 1
fi

if [[ -z "${OPENPIT_CARLA_ROOT:-}" ]]; then
  for candidate_root in \
    "${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor" \
    "${HOME}/carla"; do
    if [[ -x "${candidate_root}/CarlaUE4.sh" ]]; then
      export OPENPIT_CARLA_ROOT="${candidate_root}"
      break
    fi
  done
fi

exec "${python_bin}" "${project_dir}/scripts/check_environment.py" "$@"
