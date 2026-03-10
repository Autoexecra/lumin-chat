#!/usr/bin/env bash
# lumin-chat RPM 一键打包脚本。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"
python3 scripts/build_rpm.py "$@"
