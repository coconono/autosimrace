#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"

cleanup() {
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    deactivate || true
    echo "[tracksim] Virtual environment deactivated."
  fi
}
trap cleanup EXIT

if ! command -v python3 >/dev/null 2>&1; then
  echo "[tracksim] Error: python3 not found in PATH."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[tracksim] Creating virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$REQUIREMENTS_FILE"

cd "$PROJECT_DIR"
python -m src.tracksim.main
