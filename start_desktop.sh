#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_PYTHON:-}" ]]; then
  python_bin="${OPENPIT_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/tcp37/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/tcp37/bin/python"
else
  python_bin="$(command -v python3 || true)"
fi

if [[ -z "${python_bin}" ]]; then
  echo "ERROR: 未找到Python；请设置OPENPIT_PYTHON。" >&2
  exit 1
fi

cd "${project_dir}"
exec "${python_bin}" scripts/launch_desktop.py "$@"
