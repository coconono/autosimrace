"""Optional headless frame streaming for track_sim.

When the simulation is launched with ``ASR_STREAM=1`` (see the AutoSim Race
server setup), :class:`FrameStreamer` pipes rendered RGBA frames to a named
FIFO that an external encoder (FFmpeg) reads and pushes to YouTube.

The FIFO is opened by a daemon thread so a missing reader (e.g. FFmpeg not yet
started, or the operator using ``asr-track start`` before ``asr-stream start``)
never blocks or stalls the simulation. If the consumer is not draining fast
enough, frames are dropped rather than accumulating without bound.
"""

from __future__ import annotations

import fcntl
import os
import queue
import threading
import time


DEFAULT_FIFO = "/tmp/asr_track.fifo"


def resolve_fifo_path() -> str:
    """Return the FIFO path to stream into.

    Resolution order:
    1. ``ASR_FIFO`` environment variable.
    2. ``TRACK_FIFO`` in ``/etc/autosim/stream.conf`` (or ``ASR_STREAM_CONF``).
    3. ``/tmp/asr_track.fifo``.
    """
    path = os.environ.get("ASR_FIFO", "").strip()
    if path:
        return path

    conf = os.environ.get("ASR_STREAM_CONF", "/etc/autosim/stream.conf")
    try:
        with open(conf, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("TRACK_FIFO="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        return value
    except OSError:
        pass

    return DEFAULT_FIFO


class FrameStreamer:
    """Pushes raw RGBA frame buffers to a named FIFO for an external encoder."""

    def __init__(self, fifo_path: str, queue_size: int = 2, fps: float = 0.0) -> None:
        """fps > 0 paces output to a fixed frame rate (drops frames to stay
        constant, so the stream advances in real time even if the sim renders
        faster than the encoder target). fps == 0 disables pacing."""
        self.fifo_path = fifo_path
        self._fps = max(0.0, float(fps))
        self._frame_interval = 1.0 / self._fps if self._fps > 0 else 0.0
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Ensure the FIFO exists and begin the background writer thread."""
        if self._thread is not None:
            return
        # Create the FIFO synchronously so the file exists as soon as start()
        # returns, removing a startup race with the reader (FFmpeg).
        if not os.path.exists(self.fifo_path):
            try:
                os.mkfifo(self.fifo_path)
            except OSError:
                pass
        self._thread = threading.Thread(
            target=self._run,
            name="asr-frame-stream",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            # Probe the write end non-blocking so we never get stuck in open()
            # waiting for a reader (which would make stop() hang). If no reader
            # is attached yet, poll and honor the stop event.
            try:
                fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                self._stop.wait(0.1)
                continue

            # A reader is present. Switch to blocking I/O so each os.write can
            # deliver a WHOLE frame (a non-blocking write would only commit up
            # to the pipe-buffer size, corrupting the raw video stream).
            try:
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            except OSError:
                pass

            # Enlarge the pipe so a FULL raw frame can be staged at once
            # (Linux F_SETPIPE_SZ, best-effort). With the sim surface bounded
            # such that width*height*4 <= the pipe capacity (< the ~1 MiB
            # kernel max), each frame is delivered contiguously instead of in
            # many small blocking writes. That eliminates the partial-frame
            # corruption / "corrupt input packet" that otherwise occurs when a
            # multi-MB frame is streamed through the default 64 KiB buffer and
            # ffmpeg's reader retimes against a write boundary.
            try:
                fcntl.fcntl(fd, 1031, 1 << 20)  # F_SETPIPE_SZ = 1 MiB
            except (OSError, ValueError):
                pass

            try:
                next_slot = 0.0
                while not self._stop.is_set():
                    try:
                        frame = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if self._frame_interval > 0:
                        # Pace to a constant output rate: wait for the next slot
                        # and prefer the NEWEST ready frame (drop stale ones).
                        # This keeps the stream advancing in real time instead of
                        # letting ffmpeg timestamp a fast sim at an unreal rate.
                        now = time.monotonic()
                        if now < next_slot:
                            self._stop.wait(next_slot - now)
                        while True:
                            try:
                                frame = self._queue.get_nowait()
                            except queue.Empty:
                                break
                        now = time.monotonic()
                        next_slot = max(now, next_slot) + self._frame_interval
                    try:
                        self._write_all(fd, frame)
                    except (BrokenPipeError, OSError):
                        # Reader closed; reconnect on the next outer loop.
                        break
            finally:
                os.close(fd)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        """Write the entire frame to the blocking FIFO, handling partial writes."""
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write to FIFO")
            view = view[written:]

    def push(self, frame: bytes) -> None:
        """Queue a frame for writing.

        If the consumer is not draining, the frame is dropped so the
        simulation never stalls behind the encoder.
        """
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass

    def stop(self) -> None:
        """Stop the writer thread and release the FIFO."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None