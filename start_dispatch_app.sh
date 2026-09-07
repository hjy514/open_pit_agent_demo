#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_DISPATCH_PYTHON:-}" && -x "${OPENPIT_DISPATCH_PYTHON}" ]]; then
  python_bin="${OPENPIT_DISPATCH_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/openpit-ui/bin/python" ]]; then
  python_bin="${HOME}/miniconda3/envs/openpit-ui/bin/python"
elif [[ -x "${project_dir}/open_pit_dispatch_app/.venv/bin/python" ]]; then
  python_bin="${project_dir}/open_pit_dispatch_app/.venv/bin/python"
else
  python_bin=""
fi

if [[ -z "${python_bin}" ]]; then
  echo "ERROR: 未找到调度中心 Python；请安装 openpit-ui 环境或设置 OPENPIT_DISPATCH_PYTHON。" >&2
  exit 1
fi

export OPENPIT_DISPATCH_PYTHON="${python_bin}"
echo "[OpenPit 调度中心] Python：${python_bin}"
exec "${project_dir}/open_pit_dispatch_app/start_app.sh" "$@"
