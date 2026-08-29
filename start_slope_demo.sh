#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_path="configs/mine_competition_demo.json"
risk_config_path="configs/risk_slope_competition_synthetic.json"
monitoring_config_path="configs/monitoring_demo.json"
check_only=false

if [[ "${1:-}" == "--check-only" ]]; then
  check_only=true
  shift
fi

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

if [[ -z "${OPENPIT_CARLA_ROOT:-}" ]]; then
  for candidate_root in \
    "${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor" \
    "${HOME}/carla"; do
    if [[ -x "${candidate_root}/CarlaUE4.sh" \
      && -d "${candidate_root}/PythonAPI/carla/dist" ]]; then
      export OPENPIT_CARLA_ROOT="${candidate_root}"
      break
    fi
  done
fi

export PYTHONPATH="${project_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
runtime_sync_url="${OPENPIT_RUNTIME_API_URL:-http://127.0.0.1:8000/runtime/sync}"
api_base_url="${runtime_sync_url%/runtime/sync}"

cd "${project_dir}"

echo "[边坡失稳Demo] Python：${python_bin}"
echo "[边坡失稳Demo] CARLA目录：${OPENPIT_CARLA_ROOT:-使用场景配置}"
echo "[边坡失稳Demo] 场景：持续强降雨诱发东帮边坡异常及多矿卡任务接管"

"${python_bin}" -c '
import sys
from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config

config = load_config(sys.argv[1])
adapter = CarlaAdapter(config)
adapter._import_carla()
client = adapter.carla.Client(config.carla.host, config.carla.port)
client.set_timeout(3.0)
world = client.get_world()
current_map = world.get_map().name.split("/")[-1]
print("[边坡失稳Demo] CARLA已连接：{}:{}，当前地图={}".format(
    config.carla.host, config.carla.port, current_map
))
print("[边坡失稳Demo] 运行时将加载目标地图：{}".format(
    config.carla.map_name
))
' "${config_path}" || {
  echo "ERROR: 无法连接CARLA或加载CARLA Python API。" >&2
  echo "请先启动CARLA，并确认OPENPIT_CARLA_ROOT指向当前CARLA目录。" >&2
  exit 1
}

"${python_bin}" -c '
import json
import sys
from urllib import request

base_url = sys.argv[1]
with request.urlopen(base_url + "/", timeout=2.0) as response:
    payload = json.loads(response.read().decode("utf-8"))
if payload.get("status") != "running":
    raise SystemExit("Agent API状态异常：{}".format(payload))
print("[边坡失稳Demo] Agent API已连接：{}".format(base_url))
' "${api_base_url}" || {
  echo "ERROR: 无法连接Agent API：${api_base_url}" >&2
  echo "请先在另一终端运行 ./start_api.sh。" >&2
  exit 1
}

if [[ "${check_only}" == "true" ]]; then
  echo "[边坡失稳Demo] 启动条件检查通过，未重置API，未启动场景。"
  exit 0
fi

"${python_bin}" -c '
import sys
from urllib import request

req = request.Request(sys.argv[1] + "/runtime/reset", data=b"", method="POST")
with request.urlopen(req, timeout=2.0) as response:
    response.read()
print("[边坡失稳Demo] 已清理上一轮API运行状态和遗留指令")
' "${api_base_url}"

echo "[边坡失稳Demo] 正在启动场景……"
echo "[边坡失稳Demo] 红色风险触发后，请在调度中心人工确认接管方案。"

exec "${python_bin}" run_demo.py \
  --mode carla-run \
  --config "${config_path}" \
  --risk-config "${risk_config_path}" \
  --monitoring-config "${monitoring_config_path}" \
  --load-map \
  --spawn-missing \
  --ticks 30000 \
  "$@"
