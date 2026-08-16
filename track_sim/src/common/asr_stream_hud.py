"""HUD compositor process for the headless race stream.

Runs as its own process (``python -m src.common.asr_stream_hud``) so the leaderboard +
bottom-stats rendering, full-frame composition, and FIFO writes run on a separate core
from the renderer.

Pipeline (used when stream_show_panes=1 AND ASR_STREAM=1):

    track_sim renderer  --track-region RGBA-->  this process  --full frame-->  ffmpeg
                         --HUD snapshot JSON----------------->  (re-renders chrome)
            FIFO_A (/tmp/asr_track_region.fifo)                FIFO_B (/tmp/asr_track.fifo)

Env:
    ASR_HUD_IN_FIFO   read track frames from        (default /tmp/asr_track_region.fifo)
    ASR_HUD_OUT_FIFO  write composed frames to      (default /tmp/asr_track.fifo)
    ASR_HUD_SOCK      Unix datagram socket path     (default /tmp/asr_hud.sock)
    ASR_TRACK_CONF    path to tracksim.conf         (default <project>/etc/tracksim.conf)
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import sys
import threading
import time

from pathlib import Path

import pygame

from src.common.config import as_int
from src.common.stream_chrome import (
    compute_stream_pane_layout,
    hud_sizes,
    overlay_config,
    render_stream_chrome,
)
from src.common.streamer import FrameStreamer
from src.common.ui import create_default_font


DEFAULT_IN_FIFO = "/tmp/asr_track_region.fifo"
DEFAULT_OUT_FIFO = "/tmp/asr_track.fifo"
DEFAULT_SOCK = "/tmp/asr_hud.sock"


ENV = {
    "ASR_HUD_IN_FIFO": os.environ.get("ASR_HUD_IN_FIFO", DEFAULT_IN_FIFO),
    "ASR_HUD_OUT_FIFO": os.environ.get("ASR_HUD_OUT_FIFO", DEFAULT_OUT_FIFO),
    "ASR_HUD_SOCK": os.environ.get("ASR_HUD_SOCK", DEFAULT_SOCK),
}


def _load_scaled_logo(project_dir: Path, logo_path: str):
    """Load + scale the series logo to 80x80, or None if missing/invalid."""
    if not logo_path:
        return None
    p = Path(logo_path)
    candidates = [p]
    if not p.is_absolute():
        candidates = [project_dir / p, project_dir / "images" / "logos" / p]
    for c in candidates:
        if c.exists():
            try:
                img = pygame.image.load(str(c)).convert_alpha()
                return pygame.transform.smoothscale(img, (80, 80))
            except Exception:
                return None
    return None


def _try_open_read(path: str):
    """Open a FIFO read-only, non-blocking. Returns fd, or None if no writer yet."""
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None


def _read_exact(fd: int, n: int, stop: threading.Event, timeout: float = 0.2):
    """Read exactly n bytes, checking the stop event between waits. Returns None on EOF/stop."""
    buf = b""
    while len(buf) < n:
        if stop.is_set():
            return None
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            continue
        try:
            chunk = os.read(fd, n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _drain_sock(sock) -> dict | None:
    """Non-blocking drain of pending HUD-snapshot datagrams; return the latest or None."""
    latest = None
    while True:
        try:
            data, _ = sock.recvfrom(1 << 20)
        except BlockingIOError:
            break
        except OSError:
            break
        try:
            latest = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    return latest


def main() -> int:
    conf_path = Path(os.environ.get("ASR_TRACK_CONF", "")) if os.environ.get("ASR_TRACK_CONF") else None
    project_dir = Path(__file__).resolve().parents[2]
    if conf_path is None:
        conf_path = project_dir / "etc" / "tracksim.conf"
    conf = overlay_config(conf_path)

    w = max(2, as_int(conf, "capture_width", 640))
    h = max(2, as_int(conf, "capture_height", 360))
    panes_on = as_int(conf, "stream_show_panes", 0) == 1
    side_w, bottom_h = hud_sizes(conf, w, h)

    in_fifo = ENV["ASR_HUD_IN_FIFO"]
    out_fifo = ENV["ASR_HUD_OUT_FIFO"]
    sock_path = ENV["ASR_HUD_SOCK"]

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    pygame.init()

    canvas = pygame.display.set_mode((w, h))
    font = create_default_font(22)

    if panes_on:
        track, lb, bottom = compute_stream_pane_layout(w, h, side_w, bottom_h)
    else:
        track = pygame.Rect(0, 0, w, h)
        lb = pygame.Rect(0, 0, 0, 0)
        bottom = pygame.Rect(0, 0, 0, 0)

    frame_bytes = max(1, track.width) * max(1, track.height) * 4
    chrome_lb = pygame.Surface((max(1, lb.width), max(1, lb.height)))
    chrome_bottom = pygame.Surface((max(1, bottom.width), max(1, bottom.height)))

    logo = _load_scaled_logo(project_dir, as_conf_str(conf))

    # Datagram socket for HUD snapshots from the renderer.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    try:
        sock.bind(sock_path)
        sock.setblocking(False)
    except OSError as exc:
        print("error: cannot bind {} ({})".format(sock_path, exc), file=sys.stderr)
        return 1

    stop = threading.Event()

    def handle_signal(signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(
        "asr_stream_hud: canvas={}x{} panes={} track={}x{} -> {} -> {}".format(
            w, h, panes_on, track.width, track.height, in_fifo, out_fifo
        ),
        file=sys.stderr,
        flush=True,
    )

    out = FrameStreamer(out_fifo)
    out.start()
    # Rate-limit chrome re-rendering. The renderer publishes a HUD snapshot
    # essentially every frame (its signature tracks net_progress), and re-rendering
    # the chrome (sort ~all cars + font-fit both panes) each frame is the compositor's
    # biggest CPU cost. The HUD doesn't need 30fps freshness — the cached chrome is
    # blitted every frame regardless — so update it at most ~5x/sec.
    CHROME_REFRESH = 0.2
    last_chrome_refresh = 0.0

    def maybe_rerender(snapshot):
        """Re-render the cached chrome surfaces, but no more than once per interval."""
        nonlocal last_chrome_refresh
        if snapshot is None:
            return
        now = time.monotonic()
        if now - last_chrome_refresh < CHROME_REFRESH:
            return
        last_chrome_refresh = now
        render_stream_chrome(chrome_lb, chrome_bottom, snapshot, font, logo)

    try:
        while not stop.is_set():
            maybe_rerender(_drain_sock(sock))

            fd = _try_open_read(in_fifo)
            if fd is None:
                stop.wait(0.1)
                continue
            try:
                while not stop.is_set():
                    # Drain pending HUD snapshots + re-render chrome (rate-limited).
                    # Must be inside the per-frame loop: once a renderer is connected this
                    # inner loop runs continuously and never returns to the outer one, so
                    # otherwise snapshots would sit undrained and the chrome stays blank.
                    maybe_rerender(_drain_sock(sock))
                    frame = _read_exact(fd, frame_bytes, stop)
                    if frame is None:
                        break
                    # Compose the final full frame on the canvas.
                    track_surf = pygame.image.frombuffer(frame, (track.width, track.height), "RGBA")
                    canvas.fill((0, 0, 0))
                    canvas.blit(track_surf, (track.x, track.y))
                    if chrome_lb.get_width() > 1 and chrome_lb.get_height() > 1:
                        canvas.blit(chrome_lb, lb.topleft)
                    if chrome_bottom.get_width() > 1 and chrome_bottom.get_height() > 1:
                        canvas.blit(chrome_bottom, bottom.topleft)
                    raw = pygame.image.tobytes(canvas, "RGBA")
                    out.push(raw)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        out.stop()
        try:
            sock.close()
        except OSError:
            pass
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    return 0


def as_conf_str(conf: dict) -> str:
    """Best-effort resolve of the series_logo config into a path string."""
    return str(conf.get("series_logo", "") or "")


if __name__ == "__main__":
    sys.exit(main())
