#!/usr/bin/env bash
set -euo pipefail

# 地图资源库的统一命令入口；实现仍由 scripts/ 和 src/ 内的模块承担。
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
  candidate_root="${HOME}/carla/Dist/CARLA_Shipping_0.9.10-dirty/LinuxNoEditor"
  if [[ -x "${candidate_root}/CarlaUE4.sh" ]]; then
    export OPENPIT_CARLA_ROOT="${candidate_root}"
  fi
fi

usage() {
  cat <<'EOF'
用法：./map_resources.sh <command> [options]

常用命令：
  p5-slope                 标定边坡Demo的关键规划路线
  p5-vehicle02-candidates  标定矿卡02人工候选路线
  p5-vehicle02-scan        扫描矿卡02起点的可用目标点
  p6-slope                 实车验证边坡Demo业务路线
  p6-vehicle02             实车验证矿卡02替代路线

底层工具：
  build                    初始化/检查地图资源库
  import-topology          导入CARLA当前地图路网
  build-route-candidates   把P5可达点对关联到现有路网边
  calibrate-spawns         标定CARLA Spawn Point
  calibrate-reachability   标定路线规划可达性
  validate-physical        执行单矿卡物理路线验证
  coverage                 输出全图路线覆盖报告

所有 [options] 均透传给对应的底层工具。
EOF
}

if [[ $# -eq 0 ]]; then
  usage
  exit 0
fi

command_name="$1"
shift
cd "${project_dir}"

case "${command_name}" in
  p5-slope)
    exec "${python_bin}" scripts/calibrate_map_reachability.py \
      --route-profile configs/map_resource_slope_demo_critical_routes.json \
      --expected-map-name 0325_5 --expected-carla-version 0.9.10 "$@"
    ;;
  p5-vehicle02-candidates)
    exec "${python_bin}" scripts/calibrate_map_reachability.py \
      --route-profile configs/map_resource_vehicle02_replacement_candidates.json \
      --expected-map-name 0325_5 --expected-carla-version 0.9.10 "$@"
    ;;
  p5-vehicle02-scan)
    scan_args=(
      --from-spawn-point-index 28
      --exclude-target-spawn-point-index 5
      --exclude-target-spawn-point-index 12
      --exclude-target-spawn-point-index 48
      --exclude-target-spawn-point-index 49
      --exclude-target-spawn-point-index 63
      --exclude-target-spawn-point-index 70
      --exclude-target-spawn-point-index 78
    )
    "${python_bin}" scripts/calibrate_map_reachability.py \
      "${scan_args[@]}" \
      --expected-map-name 0325_5 --expected-carla-version 0.9.10 "$@"
    exec "${python_bin}" scripts/report_route_candidates.py "${scan_args[@]}"
    ;;
  p6-slope)
    exec "${python_bin}" scripts/validate_physical_routes.py \
      --route-profile configs/map_resource_slope_demo_physical_routes.json "$@"
    ;;
  p6-vehicle02)
    exec "${python_bin}" scripts/validate_physical_routes.py \
      --route-profile configs/map_resource_vehicle02_replacement_physical_routes.json "$@"
    ;;
  build)
    exec "${python_bin}" scripts/build_map_resource_db.py "$@"
    ;;
  import-topology)
    exec "${python_bin}" scripts/import_carla_map_topology.py "$@"
    ;;
  build-route-candidates)
    exec "${python_bin}" scripts/import_carla_map_topology.py \
      --route-candidates-only "$@"
    ;;
  calibrate-spawns)
    exec "${python_bin}" scripts/calibrate_map_spawn_points.py "$@"
    ;;
  calibrate-reachability)
    exec "${python_bin}" scripts/calibrate_map_reachability.py "$@"
    ;;
  validate-physical)
    exec "${python_bin}" scripts/validate_physical_routes.py "$@"
    ;;
  coverage)
    exec "${python_bin}" scripts/report_map_route_coverage.py "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "ERROR: 未知地图资源命令：${command_name}" >&2
    usage >&2
    exit 2
    ;;
esac
