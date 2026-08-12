#!/usr/bin/env bash
# deploy.sh — copy track_sim + asr_server scripts to the target server and run
# the server-side install.
#
# Usage:
#   ./asr_server/deploy.sh [user@host]     # default arse@arsetato.local
#
# Run from the repo root (or anywhere; it resolves paths relative to itself).
set -euo pipefail

HOST="${1:-arse@arsetato.local}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> prepare remote directories"
ssh "$HOST" 'mkdir -p ~/asr_server ~/track_sim'

echo "==> rsync track_sim -> ${HOST}:~/track_sim/"
rsync -avz --delete \
  --exclude '.venv/' \
  --exclude '.git/' \
  --exclude '**/__pycache__/' \
  --exclude 'logs/' \
  --exclude '**/*.log' \
  "$REPO_ROOT/track_sim/" "${HOST}:~/track_sim/"

echo "==> rsync asr_server scripts -> ${HOST}:~/asr_server/"
rsync -avz "$REPO_ROOT/asr_server/bin/" "${HOST}:~/asr_server/bin/"
rsync -avz "$REPO_ROOT/asr_server/etc/" "${HOST}:~/asr_server/etc/"

echo "==> running install-server.sh on ${HOST}"
ssh "$HOST" 'bash -s' < "$REPO_ROOT/asr_server/install-server.sh"

echo "==> deploy complete"