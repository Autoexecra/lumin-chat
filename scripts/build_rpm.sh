#!/usr/bin/env bash
# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

# lumin-chat RPM 一键打包脚本。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"
python3 scripts/build_rpm.py "$@"
