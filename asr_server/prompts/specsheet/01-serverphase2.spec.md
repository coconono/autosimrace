# AutoSim Race Server — Phase 2 Specsheet

> **Source prompt:** `asr_server/prompts/01-serverphase2.prompt.md`
> **Scope of this specsheet:** **Phase 2 — deploy `track_sim` and stream its live output to YouTube** (replacing the test-pattern stream from Phase 1).
> **Depends on:** `asr_server/prompts/specsheet/00-serversetup.spec.md` (host prep, FFmpeg install, `/etc/autosim/stream.env`).

---

## 1. Confidence & Constraints

| Item | Value |
| --- | --- |
| Confidence | Medium — Phase 2 touches live streaming topology shared with Phase 1 |
| Allowed scope | Server deployment, headless capture, CLI commands, source switching |
| Out of scope | Website, overlay/leaderboard visuals, 12-hour VOD-reset scheduling |
| Hardware ceiling | Time-certain realtime rendering of `track_sim` at 60fps on a Pi4 is the top risk |

> **Definition of "the stream":** the pipeline that (a) renders `track_sim` and (b) encodes/pushes it to YouTube. In Phase 1 this was a self-generated FFmpeg test pattern; in Phase 2 it must be `track_sim`'s actual rendered frames.

---

## 2. Target Host (inherited from Phase 1)

| Field | Value |
| --- | --- |
| Hostname | `arsetato.local` |
| Login user | `arse` |
| Access | SSH |
| Hardware | Raspberry Pi 4 ("potato server", see `asr_server/readme.md`) |
| OS | Linux (Raspberry Pi OS / Debian-family) |

> **Security:** never echo the SSH password or the YouTube stream key into logs/commits. Secrets remain only in `/etc/autosim/stream.env` (chmod 600, gitignored).

---

## 3. Objectives

1. Copy the `track_sim` project from the local machine to `arsetato.local` and get it running headlessly.
2. Render `track_sim` in **infinite mode** (auto-restarts a new race whenever all cars wreck).
3. Capture `track_sim`'s rendered frames and feed them to FFmpeg → YouTube (no test pattern).
4. Provide **command-line commands** to start/stop the `track_sim` output stream.
5. Provide **command-line commands** to switch the FFmpeg source between **test pattern** and **track_sim output**.

---

## 5. Phase 2 — Deploy `track_sim`

### 5.1 Copy the project to the server

From the local repo root, rsync the `track_sim/` source **excluding** the local virtualenv, git metadata, and runtime logs (they must be regenerated server-side):

```bash
rsync -avz --delete \
  --exclude '.venv/' \
  --exclude '.git/' \
  --exclude '**/__pycache__/' \
  --exclude 'logs/' \
  --exclude '**/*.log' \
  ./track_sim/ arse@arsetato.local:~/track_sim/
```

Quick verification on the server:

```bash
ssh arse@arsetato.local
ls -la ~/track_sim && ls ~/track_sim/src/tracksim
```

> `cocorp.track` and the fixed `tracks/*.track`, `cars/*.car` files **are** copied (they are not covered by the excludes above). Timestamped `track_*.track` files and logs are intentionally excluded/rebuilt.

### 5.2 System packages for headless pygame/Pillow

Installing in a venv does not pull native SDL/image libs. On the Debian-family server install `python3-venv` plus the SDL/runtime libraries payload & the ones pygame/Pillow need:

```bash
sudo apt-get install -y python3-venv \
  libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 \
  libjpeg62-turbo zlib1g
```

> **Headless note:** `SDL_VIDEODRIVER=dummy` renders offscreen in system memory, so **no X.org/desktop is required**. `SDL_AUDIODRIVER=dummy` avoids the need for an audio device.

### 5.3 Virtual environment & dependency install

```bash
cd ~/track_sim
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt   # pygame==2.6.1, Pillow>=10.0.0
python -c "import pygame, PIL; print('deps ok')"
```

**Acceptance criteria (deploy):**

- [ ] `rsync` completes without rsync errors; `src/`, `etc/`, `cars/`, `tracks/` exist on the server.
- [ ] `python -c "import pygame, PIL"` succeeds inside `.venv`.
- [ ] The simulator boots headlessly with `SDL_VIDEODRIVER=dummy` for at least a few seconds.

---

## 6. Phase 2 — Rendering Architecture

### 6.1 Headless run + infinite-mode auto-start (required small change)

`track_sim` currently enters infinite mode only via the **Race → Start Infinite Mode** menu. To satisfy *"tracksim will start in infinite mode"* the app must support an automatic start.

**Required change** to `src/tracksim/main.py`: honor an environment variable (e.g. `TRACKSIM_INFINITE=1`) or a CLI flag (`--infinite`) that, after the config is loaded and the track/cars are resolved, programmatically enters the same code path as the menu action (`infinite_mode = True; start_race_session(training=False)`), then relies on the existing all-wreck reset loop.

> The reset/hot-reload loop already exists (see `main.py`: `elif infinite_mode and not racing and race_outcome_saved:` → `_reload_car_configs(...)`, `start_race_session(training=False)`). Only the *auto-start gating* is new.

### 6.2 Frame capture (grabber)

To push rendered frames to FFmpeg, `track_sim` must expose its pixels. Preferred minimal, non-invasive design:

- New module `asr_server/grabber.py` (or `track_sim/src/common/streamer.py`) that, **only when** `ASR_STREAM=1` is set, runs a second clock tick after `pygame.display.flip()` in the main loop and writes `pygame.image.tostring(screen, "RGBA")` into the capture channel (see §7). Deterministic backpressure: if the consumer (FFmpeg) is not draining, drop frames rather than let the sim stall.

---

## 4. Phase Overview

| Phase | Goal | Deliverable | Status |
| --- | --- | --- | --- |
| 0 | Prep & hygiene | Clean baseline OS; `python3`, `pip`, `git` | ✅ done (phase 1) |
| 1 | FFmpeg streaming env | FFmpeg streams a test pattern to YouTube | ✅ done (phase 1) |
**Capture channel choice**

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Single process (FFmpeg as subprocess, `stdin=PIPE`)** | Simple; mirrors Phase-1 grabber example | Cannot stop/start stream independently; one crash kills both |
| **B. Named FIFO (`/tmp/asr_track.fifo`)** | Decouples "start/stop track_sim stream" from FFmpeg; fits the CLI-command requirement | Needs a reader to drain; raw video on FIFO lacks handshake |

**Recommended:** **Option B (FIFO)** because the prompt explicitly wants to start/stop the `track_sim` output stream and to switch the FFmpeg source independently. Raw RGBA at 1600x900 is ~33 MB/frame unencoded transport volume; see §6.3 for why the sim renders small and scales up.

### 6.3 Resolution & frame pacing (Pi4 headroom)

`track_sim` is configured (and rendered) at **1600x900, 60fps** — too heavy to encode in realtime on a Pi4.

**Design decision:** run `track_sim` at a small surface and scale in FFmpeg. Two viable shapes:

- **(Recommended)** Treat the sim surface as the *source*: set the sim window via `etc/tracksim.conf` (`window_width`/`window_height`) to a capture-friendly size (e.g. **960x540**) and pipe that at **30fps**, letting FFmpeg upscale to 1280x720 for YouTube.
- **Alternative** if inter-element text/pane legibility matters: render native 1600x900 but let FFmpeg downscale to 720p (more encoder cost).

**Recommended parameters:**
- Sim surface: `960x540` (`tracksim.conf`), sim loop `clock.tick(30)` for the streaming build.
- Encode output: `1280x720@30`, `-preset veryfast`, `-pix_fmt yuv420p`, `-g 60`, `libx264` (or `h264_v4l2m2m` if available — Phase 1 §4.1).

---

## 7. Phase 2 — FFmpeg Streaming Configuration

### 7.1 Config files

**`/etc/autosim/stream.env`** (already created in Phase 1, secret):
```ini
RTMP_URL=rtmp://a.rtmp.youtube.com/live2
STREAM_KEY=YOUR_STREAM_KEY_HERE
```

**`/etc/autosim/stream.conf`** (new — *non-secret*, switches the FFmpeg source):
```ini
# SOURCE decides what ffmpeg ingests.
#   test     = built-in lavfi test pattern + tone (Phase 1 behavior)
#   tracksim = raw RGBA frames piped from the running track_sim grabber
SOURCE=test

# Capture channel used by SOURCE=tracksim (a named pipe)
TRACK_FIFO=/tmp/asr_track.fifo

# Encode parameters
WIDTH=1280
HEIGHT=720
FPS=30
GOP=60
PRESET=veryfast
```

> Read via `source /etc/autosim/stream.conf; set -a; source /etc/autosim/stream.env; set +a` in the run scripts.

### 7.2 FFmpeg run logic (branches on `SOURCE`)

```bash
if [[ "$SOURCE" == "tracksim" ]]; then
  ffmpeg -y -f rawvideo -pix_fmt rgba -s 960x540 -r 30 \
    -i "$TRACK_FIFO" \
    -vf "scale=${WIDTH}:${HEIGHT}" \
    -c:v "$ENCODER" -preset "$PRESET" -pix_fmt yuv420p -g "$GOP" \
    -f flv "${RTMP_URL}/${STREAM_KEY}"
else  # test
  ffmpeg -re -f lavfi -i testsrc2=size=${WIDTH}x${HEIGHT}:rate=${FPS} \
    -f lavfi -i sine=frequency=440:sample_rate=44100 \
    -c:v "$ENCODER" -preset "$PRESET" -pix_fmt yuv420p -g "$GOP" \
    -c:a aac -b:a 128k \
    -f flv "${RTMP_URL}/${STREAM_KEY}"
fi
```

> `$ENCODER` = `libx264` or `h264_v4l2m2m` (detect at install, Phase 1 §4.1). When `SOURCE=tracksim` and the FIFO has no writer yet, ffmpeg will block reading — so **start order matters** (§8).

---

## 8. Phase 2 — Command-Line Control Commands

Small idempotent scripts installed in `/usr/local/bin` (mode `0755`).

### 8.1 `asr-track` — start/stop the **track_sim output stream**

```bash
asr-track start     # create FIFO, launch grabber-enabled track_sim (infinite, headless)
asr-track stop      # stop track_sim + grabber, rm FIFO
asr-track status    # show whether track_sim stream is running
```

`start` (summary):
```bash
source /etc/autosim/stream.conf
mkdir -p "$(dirname "$TRACK_FIFO")"
[[ -p "$TRACK_FIFO" ]] || mkfifo "$TRACK_FIFO"
cd ~/track_sim
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
ASR_STREAM=1 TRACKSIM_INFINITE=1 \
  nohup .venv/bin/python -m src.tracksim.main >> ~/track_sim/logs/stream.log 2>&1 &
echo $! > /run/asr-track.pid
```

`stop`:
```bash
[[ -f /run/asr-track.pid ]] && kill "$(cat /run/asr-track.pid)"
rm -f /run/asr-track.pid "$TRACK_FIFO"
```

### 8.2 `asr-stream` — control the **FFmpeg → YouTube** process

```bash
asr-stream start     # start ffmpeg using the source in /etc/autosim/stream.conf
asr-stream stop      # stop ffmpeg
asr-stream status
```

### 8.3 `asr-stream-source` — **switch** the FFmpeg source

```bash
asr-stream-source test       # set SOURCE=test
asr-stream-source tracksim   # set SOURCE=tracksim
asr-stream-source            # print current SOURCE
```

Behavior on switch:
1. Rewrite `SOURCE=` in `/etc/autosim/stream.conf`.
2. Stop the running ffmpeg (`asr-stream stop`).
3. For `tracksim`: ensure `asr-track start` is running (or warn the operator), then `asr-stream start`.
4. For `test`: `asr-stream start` (grabber optionally stopped).

### 8.4 Recommended operating sequence

```bash
asr-stream-source tracksim   # switch config to the sim output
asr-track start              # renderer producing frames into the FIFO
asr-stream start             # ffmpeg reads FIFO, pushes to YouTube
```

```bash
asr-stream-source test       # switch back to test pattern for maintenance
asr-track stop
asr-stream start
```

---

## 9. Phase 2 — Running as a Service (optional but recommended)

For 24/7 headless operation offer systemd units. Because the prompt asks for *command-line commands*, `systemctl` can back the CLI scripts (thin wrappers):

- `asr-tracksim.service` — `ExecStart` runs the grabber-enabled `track_sim` (env: `SDL_VIDEODRIVER=dummy`, `SDL_AUDIODRIVER=dummy`, `ASR_STREAM=1`, `TRACKSIM_INFINITE=1`, `EnvironmentFile=/etc/autosim/stream.conf`), `Restart=on-failure`.
- `asr-stream.service` — reworked from Phase 1 §4.4 to branch on `SOURCE` and source `/etc/autosim/stream.conf`, `EnvironmentFile=/etc/autosim/stream.env`, `Restart=on-failure`.

> `asr-track start|stop` → `systemctl start|stop asr-tracksim`; `asr-stream start|stop` → `systemctl start|stop asr-stream`. The source switch rewrites `/etc/autosim/stream.conf` then restarts `asr-stream`.

---

## 10. Logging & Verification

```bash
tail -f ~/track_sim/logs/stream.log   # sim / grabber output
journalctl -u asr-stream -f           # ffmpeg stream log
journalctl -u asr-tracksim -f
ffprobe -v error -f flv -i "${RTMP_URL}/${STREAM_KEY}"   # headcheck ingest if desired
```

**Acceptance criteria (Phase 2):**

- [ ] `track_sim` renders headlessly on `arsetato.local` with `SDL_VIDEODRIVER=dummy`.
- [ ] `TRACKSIM_INFINITE=1` auto-enters infinite mode; when all cars wreck it auto-starts a new race (existing loop).
- [ ] `asr-track start/stop/status` work; only one renderer instance is ever running.
- [ ] `asr-stream-source test|tracksim` flips `SOURCE` in `/etc/autosim/stream.conf`.
- [ ] With `SOURCE=tracksim`, the YouTube channel shows `track_sim` (not the test pattern) and is stable for ≥ 10 minutes.
- [ ] With `SOURCE=test`, the Phase-1 test pattern is restored on demand.
- [ ] Stream returns/reconnects after a process crash (Restart=on-failure) and after a reboot (enabled service).

---

## 11. Definition of Done — Phase 2

- `track_sim` copied to `arsetato.local`; `.venv` created; `pygame`/`Pillow` import successfully.
- `track_sim` runs headlessly and auto-starts infinite mode on launch.
- Command-line commands exist and are verified for:
  - starting/stopping the `track_sim` output stream;
  - switching FFmpeg from test pattern to `track_sim` output (and back).
- The YouTube live channel streams `track_sim`'s live output in real time at 720p/30 with no test pattern leaking.

---

## 12. Out of Scope (Future)

- Real-time in-stream overlay / leaderboard updates (reloads via hot-reload already exist but visual overlays are separate).
- The 12-hour VOD-reset scheduling noted in `gameplan.md`.
- Race stats website (`asr_website`) integrations.
- Optimizing the renderer for higher native resolution or 60fps encoding on the Pi4.

---

## 13. Open Questions / Assumptions

1. **Infinite-mode auto-start:** infinite mode is currently menu-only. Confirm adding `TRACKSIM_INFINITE=1` (env) and/or `--infinite` (CLI) to `src/tracksim/main.py` is acceptable, versus driving the existing UI headlessly.
2. **Capture transport:** assume **FIFO** (raw RGBA) is acceptable. If stream stability is a concern, fall back to single-process `stdin=PIPE` (Option A) at the cost of tying start/stop together.
3. **Resolution/legibility:** assume running the sim surface at **960x540@30** and upscaling to 720p is acceptable for on-stream readability; otherwise plan for a 1080p encode budget test.
4. **Hardware encoder:** recommend `h264_v4l2m2m` if present (Pi4) else `libx264 -preset veryfast`; confirm with `ffmpeg -encoders | grep v4l2m2m`.
5. **Service vs script:** CLI scripts are the deliverable; systemd units are recommended for persistence. Confirm whether to make systemd mandatory for Phase 2 done.
6. **`default_track`/`cars`:** confirm the stream should use `cocorp.track` and existing saved cars, or a dedicated always-ready track file for unattended resets.

| 2 | Race streaming env | `track_sim` deployed, running infinite & streamed to YouTube | ⬜ this document |
