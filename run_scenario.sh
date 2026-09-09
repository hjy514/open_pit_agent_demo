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
  echo "ERROR: 未找到项目Python；可通过OPENPIT_PYTHON指定。" >&2
  exit 1
fi

mode="structural"
scenario=""
check_only=false
show_help=false
forward_args=()

while (($#)); do
  case "$1" in
    --mode)
      [[ $# -ge 2 ]] || { echo "ERROR: --mode需要参数。" >&2; exit 2; }
      mode="$2"
      shift 2
      ;;
    --scenario)
      [[ $# -ge 2 ]] || { echo "ERROR: --scenario需要参数。" >&2; exit 2; }
      scenario="$2"
      forward_args+=("$1" "$2")
      shift 2
      ;;
    --check-only)
      check_only=true
      shift
      ;;
    -h|--help)
      show_help=true
      shift
      ;;
    *)
      forward_args+=("$1")
      shift
      ;;
  esac
done

if [[ "${show_help}" == "true" ]]; then
  cat <<'EOF'
OpenPit统一场景入口

用法：
  ./run_scenario.sh --scenario s01 [--mode structural] [场景参数]
  ./run_scenario.sh --scenario s01 --compare-policies --runs 10 --seed 202700
  ./run_scenario.sh --scenario s01 --mode carla --random-map [--check-only]
  ./run_scenario.sh --scenario all --mode carla --check-only
  ./run_scenario.sh --scenario s08 --mode carla [--check-only]
  ./run_scenario.sh --list-scenarios
  ./run_scenario.sh --database-health [--stale-hours 24]

当前可运行：
  structural : s01正常生产、s02车辆故障、s03设备故障、s04爆破管控、s05极端天气、s06道路拥堵、s07道路中断、s09复合扰动、all批量
  carla      : s01–s09统一场景入口；s08当前由边坡Golden兼容适配器执行

常用参数：
  --seed N --vehicle-count 6|8 --random-map --runs N
  --policy heuristic|multi-objective|auto
  --compare-policies：对S01或S02的同一组Seed执行V0/V1配对对照

数据库治理：
  --database-health只读报告；--repair-stale-runs显式修复旧的未结束Run，不删数据。

批量采集推荐--policy auto：S01/S02使用多目标调度，其余场景使用原生安全策略。
边界：multi-objective实际执行目前支持structural随机地图S01/S02；
S03/S04/S05/S06/S07/S09使用真实地图资源和结构化事件决策；S08已纳入统一目录和入口，当前复用原start_slope_demo.sh实现。
EOF
  exit 0
fi

cd "${project_dir}"

if [[ "${mode}" == "structural" ]]; then
  if [[ "${check_only}" == "true" ]]; then
    echo "ERROR: --check-only仅用于--mode carla。" >&2
    exit 2
  fi
  exec "${python_bin}" scripts/run_scenario.py "${forward_args[@]}"
fi

if [[ "${mode}" == "carla" ]]; then
  if [[ "${scenario}" =~ ^s0(1|2|3|4|5|6|7|9)$ || "${scenario}" == "all" ]]; then
    carla_args=(--mode carla "${forward_args[@]}")
    if [[ "${check_only}" == "true" ]]; then
      carla_args+=(--check-only)
    fi
    exec "${python_bin}" scripts/run_scenario.py "${carla_args[@]}"
  fi
  if [[ "${scenario}" != "s08" ]]; then
    echo "ERROR: CARLA统一入口支持s01–s07/s09；S08使用Golden Demo。" >&2
    exit 2
  fi
  # S08 is registered under the same Scenario Catalog and public command.
  # Its current execution adapter remains the fixed regression-tested Golden
  # implementation until the random multi-vehicle S08 handler is admitted.
  if ((${#forward_args[@]} != 2)); then
    echo "ERROR: S08 CARLA当前只接受--scenario s08和可选--check-only。" >&2
    exit 2
  fi
  if [[ "${check_only}" == "true" ]]; then
    exec ./start_slope_demo.sh --check-only
  fi
  exec ./start_slope_demo.sh
fi

echo "ERROR: 不支持的运行模式：${mode}；可选structural或carla。" >&2
exit 2
