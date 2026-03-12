#!/usr/bin/env bash
# Copyright (c) 2026 Autoexecra
# Licensed under the Apache License, Version 2.0.
# See LICENSE in the project root for license terms.

# lumin-chat 远端初始化脚本。
set -euo pipefail

APP_DIR="${1:-/root/lumin-chat}"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

# 远端始终使用项目内虚拟环境，避免污染系统 Python。
cd "$APP_DIR"
python3 -m venv "$VENV_DIR"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt
"$PYTHON_BIN" main.py --help
