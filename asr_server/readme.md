# AutoSim Race Server (asr_server)

Scripts and resources to set up a "potato server" (assumed Raspberry Pi 4) that runs
`track_sim` headlessly and streams its rendered output to YouTube 24/7.

Two phases are implemented:

- **Phase 1** — FFmpeg streaming environment: installs FFmpeg and streams a test
  pattern to YouTube. See `prompts/specsheet/00-serversetup.spec.md`.
- **Phase 2** — Race streaming environment: deploys `track_sim`, runs it in infinite
  mode, and streams its **live output** (instead of the test pattern) to YouTube.
  See `prompts/specsheet/01-serverphase2.spec.md`.

---

## Layout

```
asr_server/
  bin/
    asr-track            # start/stop/status the track_sim output stream
    asr-stream           # start/stop/status the FFmpeg -> YouTube stream
    asr-stream-ingest    # switch ingestion URL: primary <-> backup
    asr-stream-run       # foreground FFmpeg push (branches on SOURCE)
    asr-stream-source    # switch FFmpeg source: test <-> tracksim
  etc/
    stream.conf.template # /etc/autosim/stream.conf template (non-secret)
    asr-tracksim.service # optional systemd unit for the renderer
    asr-stream.service   # optional systemd unit for FFmpeg
  deploy.sh              # copy track_sim + scripts to server, install
  install-server.sh      # run ON the server to configure everything
  prompts/               # phase prompts + spec sheets
  readme.md
```

## How it works

`track_sim` gains two opt-in behaviors (enabled via environment variables):

- `TRACKSIM_INFINITE=1` (or `--infinite`) — auto-starts infinite mode on launch:
  loads the configured default track + its cars, then auto-restarts a new race
  whenever all cars wreck (the existing in-app loop).
- `TRACKSIM_TRAIN_FIRST=1` (or `--train-first`, combined with `TRACKSIM_INFINITE=1`)
  — runs the training/simulate phase first (`training_races` from `tracksim.conf`),
  then hands off into infinite mode once training completes. It is set by
  `asr-track` and `asr-tracksim.service` so the server trains before racing.
- `ASR_STREAM=1` — pipes each rendered frame (`RGBA`) to a named FIFO. With the HUD
  shown (`stream_show_panes=1`) the leaderboard/bottom-stats panes are rendered and
  composed by a **separate process** (`src.common.asr_stream_hud`) so that work runs on
  its own core; the renderer then only ships the track-region pixels.

So the pipeline is:

```
track_sim (headless, infinite, ASR_STREAM=1)
        |  track-region RGBA            + HUD snapshot (datagram)
        v                               v
/tmp/asr_track_region.fifo       asr_stream_hud (compositor) -> renders panes, composes
        |                                                        full frame
        v
/tmp/asr_track.fifo
        |  ffmpeg reads FIFO, scales to 720p, H.264
        v
YouTube (RTMP)
```

With `stream_show_panes=0` (pure fullscreen track) the HUD compositor is skipped and
`track_sim` writes full frames straight to `/tmp/asr_track.fifo` (one process).

`/etc/autosim/stream.conf` holds the **source selection** and encode parameters:

```ini
SOURCE=test          # test pattern (Phase 1) OR tracksim
TRACK_FIFO=/tmp/asr_track.fifo
# SRC_W/SRC_H are auto-derived from track_sim/etc/tracksim.conf for SOURCE=tracksim.
WIDTH=1280
HEIGHT=720
FPS=30
GOP=60
PRESET=veryfast
ENCODER=libx264      # or h264_v4l2m2m on a Pi4
```

The YouTube **stream key is secret** and never goes in here — it lives in
`/etc/autosim/stream.env` (chmod 600, gitignored):

```ini
RTMP_URL=rtmp://a.rtmp.youtube.com/live2          # primary ingestion URL
RTMP_BACKUP_URL=rtmp://b.rtmp.youtube.com/live2?backup=1  # backup ingestion URL
STREAM_KEY=your-real-key-here
```

`RTMP_BACKUP_URL` is the YouTube **backup** ingestion endpoint. If you ever run
two encoders against the same live stream, YouTube requires one on the primary
URL and one on the backup URL ("more than one ingestion is using the primary
URL"). Point this box at the backup with `asr-stream-ingest backup`.

---

## Deploy (run once from your local machine)

```bash
./asr_server/deploy.sh [user@host]   # default arse@arsetato.local
```

This rsyncs `track_sim/` (excluding `.venv`, `.git`, caches, logs) and the
`asr_server/bin` + `asr_server/etc` scripts to the server, then runs
`install-server.sh` which:

1. Installs `python3`, `ffmpeg`, and headless SDL/Pillow runtime libs.
2. Creates `track_sim/.venv` and installs `requirements.txt`.
3. Creates `/etc/autosim/stream.env` and `/etc/autosim/stream.conf` (first run only).
4. Installs the `asr-*` commands into `/usr/local/bin`.
5. Installs the systemd units (optional; CLI scripts are the primary interface).

> **After first deploy**, edit `/etc/autosim/stream.env` on the server and set
> your real `STREAM_KEY=`.

---

## Day-to-day commands (run on the server)

### Start/stop the track_sim output stream

```bash
asr-track start      # headless track_sim, infinite mode -> track-region FIFO
                     #   (+ auto-starts the separate HUD compositor when stream_show_panes=1)
asr-track stop
asr-track status
```

### Start/stop the FFmpeg → YouTube stream

```bash
asr-stream start
asr-stream stop
asr-stream status
```

### Switch the FFmpeg source (test pattern ⇄ track_sim)

```bash
asr-stream-source            # show current SOURCE
asr-stream-source test       # test pattern + tone
asr-stream-source tracksim   # track_sim output
```

Switching rewrites `SOURCE=` in `/etc/autosim/stream.conf`, restarts FFmpeg, and
(for `tracksim`) auto-starts the renderer if it isn't running.

### Switch the ingestion URL (primary ⇄ backup)

```bash
asr-stream-ingest            # show current INGEST (primary/backup)
asr-stream-ingest primary    # push to RTMP_URL  (rtmp://a.rtmp.youtube.com/live2)
asr-stream-ingest backup     # push to RTMP_BACKUP_URL (rtmp://b.rtmp.youtube.com/live2?backup=1)
```

Rewrites `INGEST=` in `/etc/autosim/stream.conf` and restarts FFmpeg.

### Recommended operating sequence

Stream the race:

```bash
asr-stream-source tracksim
asr-track start
asr-stream start
```

Go back to the test pattern for maintenance:

```bash
asr-stream-source test
asr-track stop
asr-stream start
```

---

## Logs & verification

```bash
tail -f ~/track_sim/logs/stream.log       # sim / grabber (renderer) output
tail -f ~/track_sim/logs/stream-hud.log   # HUD compositor output (when stream_show_panes=1)
tail -f ~/.asr/asr-stream.log             # ffmpeg output (timestamped, keeps last 100 lines)
ffmpeg -encoders | grep v4l2m2m           # confirm Pi4 hardware encoder (optional)
```

---

## Notes / tuning

- **Resolution & capture:** two separate sizes. `window_width`/`window_height`
  in `track_sim/etc/tracksim.conf` is the **interactive render size** the UI lays
  out at (1280x720). `capture_width`/`capture_height` is the **streaming frame
  size**: under the headless `ASR_STREAM=1` build the renderer draws directly at
  this size (repo default 960x540) and `asr-stream-run` reads the same value to
  feed ffmpeg's rawvideo `-s`, then **upscales to 720p**. That upscale is why we
  can drop the capture below 720p to cut the renderer's pixel work (~45% fewer
  pixels at 960x540; ~75% at 640x360). **`capture_*` must stay in sync** between
  the renderer and `asr-stream-run` — the raw RGBA stream has no in-band framing,
  so a mismatch makes ffmpeg read misaligned frames → a doubled/garbled image.
  Because the leaderboard/bottom-stats panes are sized in absolute capture pixels
  (`stream_leaderboard_width` / `stream_bottom_stats_height`), lowering the capture
  without scaling those sizes makes the HUD dominate the frame — the repo scales
  them proportionally (0.75x at 960x540). Keeping the capture smaller gives the Pi
  enough headroom to render and push more distinct frames/sec, which is what
  actually makes the stream's motion smooth — raise `capture_*` goes the other way
  (sharper but choppier).
- **Encoder:** software `libx264 -preset veryfast` by default; on a Pi4 switch
  `ENCODER=h264_v4l2m2m` if available.
- **Backpressure:** the grabber drops frames, never blocks the simulation.
- The systemd units are a persistent alternative:
  `sudo systemctl enable --now asr-tracksim asr-stream`.