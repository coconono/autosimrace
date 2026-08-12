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
- `ASR_STREAM=1` — pipes each rendered frame (`RGBA`) to a named FIFO
  (`/tmp/asr_track.fifo` by default) read by FFmpeg. A background thread writes
  the FIFO with backpressure, so a missing reader never stalls the sim.

So the pipeline is:

```
track_sim (headless, infinite, ASR_STREAM=1)
        |  raw RGBA frames
        v
/tmp/asr_track.fifo
        |  ffmpeg reads FIFO, scales to 720p, H.264
        v
YouTube (RTMP)
```

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
RTMP_URL=rtmp://a.rtmp.youtube.com/live2
STREAM_KEY=your-real-key-here
```

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
asr-track start      # headless track_sim, infinite mode, grabber -> FIFO
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
tail -f ~/track_sim/logs/stream.log   # sim / grabber output
tail -f ~/.asr/asr-stream.log       # ffmpeg output
ffmpeg -encoders | grep v4l2m2m       # confirm Pi4 hardware encoder (optional)
```

---

## Notes / tuning

- **Resolution & capture:** two separate sizes now. `window_width`/`window_height`
  in `track_sim/etc/tracksim.conf` is the **render size** the sim UI lays out at
  (larger = panels/menus fit; repo default 1280x720). `capture_width`/`capture_height`
  is the **streaming frame size** written to the FIFO (repo default 640x360): the
  renderer downscales each rendered frame with `pygame.transform.smoothscale`
  before pushing it, so a full RGBA frame (922 KB) fits the enlarged ~1 MiB pipe
  buffer (set via `F_SETPIPE_SZ` in `src/common/streamer.py`) — that gives atomic
  frame delivery and avoids the partial-frame corruption ("corrupt input packet")
  caused by bigger frames on the 64 KiB default buffer. FFmpeg auto-derives its
  capture size from `capture_width`/`capture_height` and upscales to 720p. If you
  raise capture_*, keep `capture_width*height*4 <= ~1 MiB` for reliable delivery.
- **Encoder:** software `libx264 -preset veryfast` by default; on a Pi4 switch
  `ENCODER=h264_v4l2m2m` if available.
- **Backpressure:** the grabber drops frames, never blocks the simulation.
- The systemd units are a persistent alternative:
  `sudo systemctl enable --now asr-tracksim asr-stream`.