#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/root/github-copilot-moniter}"
VENV_DIR="$APP_DIR/.venv"

cd "$APP_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py --help
