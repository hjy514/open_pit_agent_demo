#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${project_dir}/open_pit_dispatch_app/start_app.sh" "$@"