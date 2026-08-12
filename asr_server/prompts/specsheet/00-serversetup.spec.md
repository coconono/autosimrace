# AutoSim Race Server — Setup Specsheet

> **Source prompt:** `asr_server/prompts/00-serversetup.prompt.md`
> **Scope of this specsheet:** **Phase 1 — the FFmpeg streaming environment.**
> Phase 2 (the autosim race server environment) is tracked but out of scope here.

---

## 1. Target Host

| Field | Value |
| --- | --- |
| Hostname | `arsetato.local` |
| Login user | `arse` |
| Access | SSH |
| Assumed hardware | Raspberry Pi 4 ("potato server", see `asr_server/readme.md`) |
| OS | Linux (Raspberry Pi OS / Debian-family assumed) |

> **Security note:** `arsetato.local` / `arse` come from the prompt. Do not echo the login
> password in logs or commits; store secrets in `/etc/autosim/stream.env` (chmod 600),
> which is gitignored.

---

## 2. Phase Overview

| Phase | Goal | Deliverable | Status |
| --- | --- | --- | --- |
| 0 | Prep & hygiene | Clean baseline OS; `python3`, `pip`, `git` installed | ⬜ pending |
| 1 | FFmpeg streaming env | FFmpeg installed; streams a test pattern to YouTube over RTMP | ⬜ pending |
| 2 | AutoSim race server env | Race project deployed & running on loop | ⬜ future / out of scope |

**This document implements Phase 0 + Phase 1.**

---

## 3. Phase 0 — Prerequisites & System Hygiene

### 3.1 Required Packages

Install the base runtime tooling:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3 python3-pip git
```

### 3.2 Package Cleanup (remove unnecessary packages/dependencies)

The server should be lean. Remove GUI, games, media viewers, and orphaned deps:

```bash
# Remove desktop/pinning packages that a headless server doesn't need
sudo apt-get remove --purge -y raspberrypi-ui-mods raspberrypi-desktop \
    libreoffice* thonny scratch* minecraft-pi wolfram-engine

# Remove now-orphaned dependencies
sudo apt-get autoremove -y --purge

# Clear cached package archives
sudo apt-get clean
```

**Acceptance criteria (Phase 0):**

- [ ] `python3 --version` succeeds.
- [ ] `pip3 --version` succeeds.
- [ ] `git --version` succeeds.
- [ ] `autoremove` reports 0 removable packages.

---

## 4. Phase 1 — FFmpeg Streaming Environment

### 4.1 Install FFmpeg

```bash
sudo apt-get install -y ffmpeg
ffmpeg -version   # verify
```

Required FFmpeg features (confirm present):

- **Encoder:** H.264 (`libx264`) — `ffmpeg -encoders | grep x264`
- **Muxer:** FLV (for YouTube RTMP ingest) — `ffmpeg -muxers | grep flv`
- **Filters:** `testsrc2`, `sine` (for test pattern and tone)

Optional but recommended: enable hardware encoding on Pi4 (`h264_v4l2m2m`) for headroom:

```bash
ffmpeg -encoders | grep v4l2m2m   # confirm hardware encoder present
```

### 4.2 YouTube Stream / Secret Handling

Do **not** hardcode the stream key. Store it in a gitignored, closed-perm env file:

```bash
sudo mkdir -p /etc/autosim && sudo chmod 700 /etc/autosim
sudo tee /etc/autosim/stream.env > /dev/null << 'EOF'
RTMP_URL=rtmp://a.rtmp.youtube.com/live2
STREAM_KEY=YOUR_STREAM_KEY_HERE
EOF
sudo chmod 600 /etc/autosim/stream.env
```

> For a local manual run, source it: `set -a; source /etc/autosim/stream.env; set +a`

### 4.2.1 Getting the YouTube Stream Key

The stream key lives in **YouTube Studio**. Steps:

1. Sign in to the Google/YouTube account that owns the channel on youtube.com.
2. Click your profile avatar (top-right) → **YouTube Studio**.
3. In the left sidebar, click **Content** → then the **Live** tab (or **Create → Go live**).
4. Click **Manage** under **Streams** (opens the Live Control Room).
5. In the Live Control Room, click the **Stream** tab.
6. Under **Stream Settings**, the **Stream key** is shown (a long string, usually `XXXX-XXXX-XXXX-XXXX-XXXX`). Click the **Copy** icon.
7. The **Stream URL** is shown above/below the key — for RTMP it is typically `rtmp://a.rtmp.youtube.com/live2`. If only the numeric stream key is shown, the URL is the default above.

Paste the copied key into `STREAM_KEY=...` in `/etc/autosim/stream.env`:

```bash
sudo nano /etc/autosim/stream.env
# STREAM_KEY=jax8-1hvc-7xyp-640p-ew9j
```

Notes:

- The stream key is **secret** — anyone with it can broadcast to your channel. Never commit it to git.
- For a **private test**, set the stream visibility to **Unlisted** or **Private** so only you can view the test pattern.
- The key can be **rotated** (reset) from the same Live Control Room → **Reset** if it leaks.
- YouTube requires the stream to be **linked** to a live stream before receiving; with a persistent ingest key you can just start streaming and it will appear once the encoder connects.

### 4.3 Test-Pattern Stream Command

Generates a test card + tone and pushes it to YouTube indefinitely:

```bash
bash -c 'set -a; source /etc/autosim/stream.env; set +a; \
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
       -f lavfi -i sine=frequency=440:sample_rate=44100 \
       -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 60 \
       -c:a aac -b:a 128k \
       -f flv rtmp://a.rtmp.youtube.com/live2/$STREAM_KEY'
```

**Parameter notes (Pi4 headroom):**

- `1280x720@30` is the baseline; only raise to `1080p` if CPU headroom allows.
- `-preset veryfast` tuned for low-end hardware.
- `-g 60` = a keyframe every 2s (recommended for YouTube ingest).
- If using hardware encode, swap `-c:v libx264` for `-c:v h264_v4l2m2m` and drop `-pix_fmt yuv420p` (m2m handles it).

### 4.4 Run as a Service (survives reboot / headless)

Create `/etc/systemd/system/asr-stream.service`:

```ini
[Unit]
Description=AutoSim Race stream service (test pattern)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/autosim/stream.env
ExecStart=/usr/bin/ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:sample_rate=44100 \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 60 \
  -c:a aac -b:a 128k \
  -f flv ${RTMP_URL}/${STREAM_KEY}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now asr-stream
sudo systemctl status asr-stream
```

### 4.5 Logging

```bash
journalctl -u asr-stream -f       # follow live
journalctl -u asr-stream -n 200   # recent history
```

**Acceptance criteria (Phase 1):**

- [ ] `ffmpeg -version` prints and `libx264` encoder is present.
- [ ] Test pattern (`testsrc2` card) is visible on the YouTube live channel.
- [ ] Audio tone is audible in the stream.
- [ ] Service restarts automatically after a crash (`Restart=on-failure`).
- [ ] Stream returns after a reboot (service enabled, `multi-user.target`).

---

## 5. Definition of Done — Phase 1

- Baseline OS is clean (unnecessary packages removed).
- `python3`, `pip`, `git` installed and verified.
- FFmpeg installed with `libx264` (and HW encoder if available).
- Stream key stored securely (not committed to git).
- A 720p test-pattern + tone stream is pushed to YouTube and confirmed stable.
- A systemd service runs the stream headlessly and survives reboots.

---

## 6. Out of Scope (Future Phase 2)

- Deploying the autosim race project to `arsetato.local`.
- Staging / running scripts for the race project (see `asr_server/readme.md`).
- Pygame grabber that captures the race renderer and feeds it to FFmpeg.
- Stream reset scheduling (the 12-hour VOD reset noted in `gameplan.md`).
- Overlay / leaderboard updates for in-stream.

---

## 7. Open Questions / Assumptions

- Confirm Pi4 hardware encoder availability at install time; fall back to `libx264` if absent.
- Confirm whether Phase 2 reuses the same systemd service mechanism for the race process.
- Confirm whether the login password should rotate / be stored outside this repo.
