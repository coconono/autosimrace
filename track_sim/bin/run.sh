#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"

cleanup() {
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    deactivate || true
    echo "[track_sim] Virtual environment deactivated."
  fi
}

trap cleanup EXIT

echo "[track_sim] Project directory: $PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[track_sim] Error: python3 not found in PATH."
  echo "[track_sim] Install Python 3.9+ and ensure python3 is available."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[track_sim] Creating virtual environment at $VENV_DIR"
  if ! python3 -m venv "$VENV_DIR"; then
    echo "[track_sim] Error: failed to create virtual environment."
    echo "[track_sim] Ensure your Python installation includes venv support."
    exit 1
  fi
else
  echo "[track_sim] Reusing existing virtual environment."
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "[track_sim] Virtual environment activated."

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "[track_sim] Error: requirements file not found at $REQUIREMENTS_FILE"
  exit 1
fi

echo "[track_sim] Installing dependencies from requirements.txt"
python -m pip install --upgrade pip
python -m pip install -r "$REQUIREMENTS_FILE"

echo "[track_sim] Starting application..."
cd "$PROJECT_DIR"
python -m src.tracksim.main
