#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${OPENPIT_PYTHON:-}" ]]; then
  PYTHON_BIN="${OPENPIT_PYTHON}"
elif [[ -x "${HOME}/miniconda3/envs/tcp37/bin/python" ]]; then
  PYTHON_BIN="${HOME}/miniconda3/envs/tcp37/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "ERROR: 未找到Python；请设置OPENPIT_PYTHON。" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${PROJECT_DIR}/scripts/check_latest_run.py" "$@"
