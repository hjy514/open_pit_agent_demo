#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_DISPATCH_PYTHON:-}" && -x "${OPENPIT_DISPATCH_PYTHON}" ]]; then
  python_bin="${OPENPIT_DISPATCH_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/openpit-ui/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/openpit-ui/bin/python"
elif [[ -x "${project_dir}/.venv/bin/python" ]]; then
  python_bin="${project_dir}/.venv/bin/python"
else
  python_bin="$(command -v python3 || true)"
fi

if [[ -z "${python_bin}" || ! -x "${python_bin}" ]]; then
  echo "ERROR: 未找到Python；请设置OPENPIT_DISPATCH_PYTHON。" >&2
  exit 1
fi

cd "${project_dir}"
exec "${python_bin}" main.py "$@"
