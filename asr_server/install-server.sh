#!/usr/bin/env bash
# install-server.sh — configure the target server for the track_sim stream.
#
# Run ON the target server (also invoked automatically by deploy.sh).
# Idempotent: safe to re-run.
set -euo pipefail

# --- Base tooling + headless pygame/Pillow runtime libraries ---
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg git \
  libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
  libjpeg62-turbo zlib1g

# --- track_sim virtualenv & dependencies ---
cd "$HOME/track_sim"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import pygame, PIL; print('deps ok')"

# --- /etc/autosim configuration ---
sudo mkdir -p /etc/autosim
if [[ ! -f /etc/autosim/stream.env ]]; then
  echo "Creating /etc/autosim/stream.env (edit it with your REAL stream key)."
  printf 'RTMP_URL=rtmp://a.rtmp.youtube.com/live2\nRTMP_BACKUP_URL=rtmp://b.rtmp.youtube.com/live2?backup=1\nSTREAM_KEY=YOUR_STREAM_KEY_HERE\n' \
    | sudo tee /etc/autosim/stream.env >/dev/null
  sudo chmod 600 /etc/autosim/stream.env
fi
if [[ ! -f /etc/autosim/stream.conf ]]; then
  sudo cp "$HOME/asr_server/etc/stream.conf.template" /etc/autosim/stream.conf
fi

# Fix ownership/perms for /etc/autosim. Creating it with sudo leaves it
# root-owned mode 700 (drwx------), which the day-to-day asr-* commands (run
# unprivileged as this operator account) cannot read. Give the operator account
# access while keeping the files non-world-readable. Re-applied each run
# (idempotent).
OPERATOR_USER="$(id -un)"
OPERATOR_GROUP="$(id -gn)"
sudo chown -R "$OPERATOR_USER":"$OPERATOR_GROUP" /etc/autosim
sudo chmod 750 /etc/autosim
sudo chmod 640 /etc/autosim/stream.env
sudo chmod 640 /etc/autosim/stream.conf

# --- Install CLI commands ---
sudo install -m 0755 "$HOME/asr_server/bin/asr-track" /usr/local/bin/asr-track
sudo install -m 0755 "$HOME/asr_server/bin/asr-stream" /usr/local/bin/asr-stream
sudo install -m 0755 "$HOME/asr_server/bin/asr-stream-run" /usr/local/bin/asr-stream-run
sudo install -m 0755 "$HOME/asr_server/bin/asr-stream-source" /usr/local/bin/asr-stream-source
sudo install -m 0755 "$HOME/asr_server/bin/asr-stream-ingest" /usr/local/bin/asr-stream-ingest

# --- systemd units (alternative to the CLI scripts) ---
sudo install -m 0644 "$HOME/asr_server/etc/asr-tracksim.service" /etc/systemd/system/asr-tracksim.service
sudo install -m 0644 "$HOME/asr_server/etc/asr-stream.service" /etc/systemd/system/asr-stream.service
sudo systemctl daemon-reload

echo "==> install complete."
echo "    Edit /etc/autosim/stream.env with your YouTube stream key (STREAM_KEY=)."
echo "    Then:  asr-stream-source tracksim && asr-track start && asr-stream start"