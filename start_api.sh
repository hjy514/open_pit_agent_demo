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
  python_bin="$(command -v python3 || true)"
fi

if [[ -z "${python_bin}" || ! -x "${python_bin}" ]]; then
  echo "ERROR: 未找到Agent Python；请设置OPENPIT_PYTHON。" >&2
  exit 1
fi

export PYTHONPATH="${project_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
cd "${project_dir}"

echo "[OpenPit API] 项目目录：${project_dir}"
echo "[OpenPit API] Python：${python_bin}"
"${python_bin}" -c '
import open_pit_agent.interfaces.api_server as module

routes = sorted(route.path for route in module.app.routes)
print("[OpenPit API] 加载模块：{}".format(module.__file__))
print("[OpenPit API] 接口版本：{}".format(module.app.version))
print("[OpenPit API] /monitoring：{}".format(
    "已加载" if "/monitoring" in routes else "缺失"
))
if "/monitoring" not in routes:
    raise SystemExit("ERROR: 当前模块缺少 /monitoring 接口")
'

exec "${python_bin}" -m uvicorn \
  open_pit_agent.interfaces.api_server:app \
  --host 127.0.0.1 \
  --port 8000
