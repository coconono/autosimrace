"""Shared streaming HUD (\"stats display\") logic for track_sim.

In the ASR_STREAM build the leaderboard + bottom-stats overlay is rendered by a
separate ``asr_stream_hud`` process (run via ``python -m src.common.asr_stream_hud``)
so the heavy font-fitting/stat work leaves the renderer's single thread and lands on
another core. To share one implementation between the renderer and that process, the
chrome is drawn from a plain, JSON-serialisable *snapshot* dict instead of live model
objects:

``build_hud_snapshot`` produces it from sim cars; the renderer serialises it over a Unix
datagram socket to the HUD process, which feeds it back into ``render_stream_chrome``.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pygame

from src.common.config import as_int, read_simple_conf
from src.common.ui import (
    create_default_font,
    draw_lines,
    draw_lines_fit,
    draw_lines_fit_segmented,
)

# Bright standout color used for car-name text embedded in HUD/stats lines. Mirror of
# track_sim/main.py's constant, kept local so this module is self-containing and does not
# depend on the (interactive-centric) renderer module.
CAR_NAME_ACCENT = (255, 200, 60)

# Pane palette — matches the loading-screen look: black background, grass-green
# text.  CAR_NAME_ACCENT (gold) is kept for car-name segments.
PANE_BG = (0, 0, 0)
PANE_BORDER = (42, 145, 75)
PANE_TEXT = (42, 145, 75)


def compute_stream_pane_layout(w: int, h: int, side_w: int, bottom_h: int):
    """Return (track, leaderboard, bottom) rects for the streaming HUD layout.

    The race (track) pane takes everything not given to the right-hand leaderboard column
    and the full-width bottom stats strip. Centralising this here keeps the renderer
    (which ships the track pane) and the HUD overlay (which composites it) agreeing on the
    exact pixel geometry.
    """
    track = pygame.Rect(0, 0, w - side_w, h - bottom_h)
    leaderboard = pygame.Rect(w - side_w, 0, side_w, h - bottom_h)
    bottom = pygame.Rect(0, h - bottom_h, w, bottom_h)
    return track, leaderboard, bottom


def _lap_record(rec) -> dict | None:
    """Normalise a SeriesLapRecord to a JSON-friendly dict (None-safe)."""
    if rec is None:
        return None
    return {
        "car_name": rec.car_name,
        "race_number": rec.race_number,
        "lap_time": rec.lap_time,
    }


def build_hud_snapshot(sim_cars, series_name: str, series_completed_races: int, series_race_target: int) -> dict:
    """Serialise the live sim state into the plain snapshot dict the HUD renders from.

    Reads attributes by duck-typing (getattr + object access), so it accepts the
    renderer's SimCar model without importing it.
    """
    cars: list[dict] = []
    for e in sim_cars:
        st = getattr(e, "state", None)
        ss = getattr(e, "series_stats", None)
        cars.append(
            {
                "car_number": getattr(e, "car_number", 0),
                "instance_name": getattr(e, "instance_name", str(e)),
                "laps": st.laps if st is not None else 0,
                "net_progress": st.net_progress if st is not None else 0.0,
                "points": ss.points if ss is not None else 0,
                "max_race_speed": getattr(e, "max_race_speed", 0.0),
                "best_lap_seconds": getattr(e, "best_lap_seconds", 0.0),
                "state": st.state if st is not None else "unknown",
                "speed": st.speed if st is not None else 0.0,
                "fuel": st.fuel if st is not None else 0.0,
                "tire_health": st.tire_health if st is not None else 0.0,
                "damage": st.damage if st is not None else 0.0,
                "max_drift_duration": getattr(e, "max_drift_duration", 0.0),
                "first_crash_time": getattr(e, "first_crash_time", None),
                "last_contact_time": getattr(e, "last_contact_time", None),
                "fastest_lap": _lap_record(ss.fastest_lap) if ss is not None else None,
                "slowest_lap": _lap_record(ss.slowest_lap) if ss is not None else None,
            }
        )
    return {
        "series_name": series_name,
        "series_completed_races": series_completed_races,
        "series_race_target": series_race_target,
        "cars": cars,
    }


def stream_chrome_signature(snapshot: dict) -> str:
    """Compact string capturing everything the HUD displays.

    Cheap to rebuild each frame; used to detect when the cached chrome overlay actually
    needs re-rendering (and, in the split setup, when the renderer should push a new
    snapshot to the HUD process).
    """
    parts: list[str] = [
        "{}|{}|{}".format(
            snapshot["series_name"],
            snapshot["series_completed_races"],
            snapshot["series_race_target"],
        )
    ]
    for e in sorted(snapshot["cars"], key=lambda c: c["net_progress"], reverse=True):
        parts.append(
            "{}|{}|{}|{:.0f}|{}|{:.1f}|{:.2f}".format(
                e["car_number"],
                e["instance_name"],
                e["laps"],
                e["net_progress"],
                e["points"],
                e["max_race_speed"],
                e["best_lap_seconds"],
            )
        )
    return "\n".join(parts)



def _best_worst_lap(cars: list[dict]):
    """Return (best, worst) (car name, lap seconds) across the snapshot cars."""
    laps = [(c["instance_name"], c["best_lap_seconds"]) for c in cars if c["best_lap_seconds"] > 0.0]
    if not laps:
        return None, None
    return min(laps, key=lambda t: t[1]), max(laps, key=lambda t: t[1])


def render_stream_chrome(
    lb_surface,
    bottom_surface,
    snapshot: dict,
    font,
    series_logo_scaled,
) -> None:
    """Fill and draw the leaderboard + bottom-stats panes into their own OPAQUE cached
    surfaces. Each surface is pane-sized and is blitted beside the track (the track pane is
    already shrunk to leave room), so the chrome never overlaps the race. Re-rendered only
    when the standings change.

    When snapshot["loading"] is True, both panes are filled with black (0,0,0)
    to seamlessly match the simulation pane background and the entire canvas
    appears uniform during training/loading.
    """
    if snapshot.get("loading"):
        # Suppress chrome: fill panes with black (0,0,0) so they seamlessly
        # match the simulation pane background during training/loading.
        lb_surface.fill((0, 0, 0))
        bottom_surface.fill((0, 0, 0))
        return

    cars = snapshot["cars"]
    series_name = snapshot["series_name"]
    series_completed_races = snapshot["series_completed_races"]
    series_race_target = snapshot["series_race_target"]

    # Small header font for the narrow/short HUD panes.
    hfont = create_default_font(12)
    # --- Leaderboard pane ---
    lb_surface.fill(PANE_BG)
    pygame.draw.rect(lb_surface, PANE_BORDER, lb_surface.get_rect(), width=1)
    draw_lines(lb_surface, hfont, ["Leaderboard"], 8, 6, PANE_TEXT)
    lb_y = 34
    ordered = sorted(cars, key=lambda c: c["net_progress"], reverse=True)
    lb_segments: list[tuple[str, str, str]] = []
    for pos, entry in enumerate(ordered[:12], start=1):
        cn = entry["car_number"]
        prefix = "{}. ".format(pos) + ("#{} ".format(cn) if cn > 0 else "")
        name = entry["instance_name"][:16] if cn > 0 else entry["instance_name"][:18]
        suffix = "  L{}  {}p".format(entry["laps"], entry["points"])
        lb_segments.append((prefix, name, suffix))
    draw_lines_fit_segmented(
        lb_surface,
        lb_segments,
        6,
        lb_y,
        PANE_TEXT,
        CAR_NAME_ACCENT,
        max_width=lb_surface.get_width() - 12,
        line_height=20,
        start_size=15,
        min_size=9,
    )

    # --- Bottom stats pane ---
    bw = bottom_surface.get_width()
    bottom_surface.fill(PANE_BG)
    pygame.draw.rect(bottom_surface, PANE_BORDER, bottom_surface.get_rect(), width=1)

    ss1_x = 10
    ss1_y = 8
    ss1_w = 300
    draw_lines(bottom_surface, font, ["Series Stats"], ss1_x, ss1_y, PANE_TEXT)
    series_segments: list[tuple[str, str, str]] = [
        ("Name: {}".format(series_name) if series_name else "Name: (none)", "", ""),
        (
            "Races: {}/{}".format(
                series_completed_races, series_race_target if series_race_target > 0 else "∞"
            ),
            "",
            "",
        ),
    ]
    all_fastest = [c["fastest_lap"] for c in cars if c["fastest_lap"]]
    all_slowest = [c["slowest_lap"] for c in cars if c["slowest_lap"]]
    if all_fastest:
        fr = min(all_fastest, key=lambda r: r["lap_time"])
        series_segments.append(("Fast: ", fr["car_name"][:12], " R{} {:.2f}s".format(fr["race_number"], fr["lap_time"])))
    else:
        series_segments.append(("Fast: --", "", ""))
    if all_slowest:
        sr = max(all_slowest, key=lambda r: r["lap_time"])
        series_segments.append(("Slow: ", sr["car_name"][:12], " R{} {:.2f}s".format(sr["race_number"], sr["lap_time"])))
    else:
        series_segments.append(("Slow: --", "", ""))
    draw_lines_fit_segmented(
        bottom_surface,
        series_segments,
        ss1_x, ss1_y + 28,
        PANE_TEXT, CAR_NAME_ACCENT,
        max_width=ss1_w, line_height=20, start_size=16, min_size=10,
    )

    # Series logo in series stats 1.
    if series_logo_scaled is not None:
        logo_size = 80
        bottom_surface.blit(series_logo_scaled, (ss1_x + ss1_w - logo_size - 4, ss1_y + 4))

    # Points leaders (top 5).
    ss2_x = ss1_x + ss1_w + 10
    ss2_w = 240
    draw_lines(bottom_surface, font, ["Points Leaders"], ss2_x, ss1_y, PANE_TEXT)
    if cars:
        leaders = sorted(cars, key=lambda c: c["points"], reverse=True)[:5]
        pl_y = ss1_y + 28
        pl_segments: list[tuple[str, str, str]] = []
        for rank, entry in enumerate(leaders, start=1):
            cn = entry["car_number"]
            prefix = "{}. ".format(rank) + ("#{} ".format(cn) if cn > 0 else "")
            name = entry["instance_name"][:12] if cn > 0 else entry["instance_name"][:14]
            suffix = " - {}pts".format(entry["points"])
            pl_segments.append((prefix, name, suffix))
        draw_lines_fit_segmented(
            bottom_surface, pl_segments, ss2_x, pl_y,
            PANE_TEXT, CAR_NAME_ACCENT,
            max_width=ss2_w, line_height=20, start_size=16, min_size=10,
        )

    # Race stats 1.
    rs1_x = ss2_x + ss2_w + 10
    rs1_w = 260
    draw_lines(bottom_surface, font, ["Race Stats"], rs1_x, ss1_y, PANE_TEXT)
    leader_entry = max(cars, key=lambda c: c["net_progress"]) if cars else None
    top_speed_entry = max(cars, key=lambda c: c["max_race_speed"]) if cars else None
    leader_laps = max((c["laps"] for c in cars), default=0)
    best, worst = _best_worst_lap(cars)
    race1_segments: list[tuple[str, str, str]] = []
    if leader_entry:
        race1_segments.append(("Leader: ", leader_entry["instance_name"][:16], ""))
    else:
        race1_segments.append(("Leader: --", "", ""))
    if top_speed_entry:
        race1_segments.append(("Top Spd: ", top_speed_entry["instance_name"][:12], " {:.0f}".format(top_speed_entry["max_race_speed"])))
    else:
        race1_segments.append(("Top Spd: --", "", ""))
    race1_segments.append(("Laps: {}".format(leader_laps), "", ""))
    if best:
        race1_segments.append(("Fast: {:.2f}s (".format(best[1]), best[0][:10], ")"))
    else:
        race1_segments.append(("Fast: --", "", ""))
    if worst:
        race1_segments.append(("Slow: {:.2f}s (".format(worst[1]), worst[0][:10], ")"))
    else:
        race1_segments.append(("Slow: --", "", ""))
    draw_lines_fit_segmented(
        bottom_surface, race1_segments, rs1_x, ss1_y + 28,
        PANE_TEXT, CAR_NAME_ACCENT,
        max_width=rs1_w, line_height=20, start_size=16, min_size=10,
    )

    # Race stats 2 (drift, crash, contact).
    rs2_x = rs1_x + rs1_w + 10
    rs2_w = 280
    draw_lines(bottom_surface, font, ["Race Stats 2"], rs2_x, ss1_y, PANE_TEXT)
    drift_car = max(cars, key=lambda c: c["max_drift_duration"]) if cars else None
    crashed = [c for c in cars if c["first_crash_time"] is not None]
    contacted = [c for c in cars if c["last_contact_time"] is not None]
    crash_car = min(crashed, key=lambda c: c["first_crash_time"]) if crashed else None
    contact_car = max(contacted, key=lambda c: c["last_contact_time"]) if contacted else None
    race2_segments: list[tuple[str, str, str]] = []
    if drift_car and drift_car["max_drift_duration"] > 0:
        race2_segments.append(("Longest Drift: ", drift_car["instance_name"][:12], " {:.1f}s".format(drift_car["max_drift_duration"])))
    else:
        race2_segments.append(("Longest Drift: --", "", ""))
    if crash_car:
        race2_segments.append(("Quick Crash: ", crash_car["instance_name"][:12], " {:.1f}s".format(crash_car["first_crash_time"])))
    else:
        race2_segments.append(("Quick Crash: --", "", ""))
    if contact_car:
        race2_segments.append(("Last Contact: ", contact_car["instance_name"][:12], " {:.1f}s".format(contact_car["last_contact_time"])))
    else:
        race2_segments.append(("Last Contact: --", "", ""))
    draw_lines_fit_segmented(
        bottom_surface, race2_segments, rs2_x, ss1_y + 28,
        PANE_TEXT, CAR_NAME_ACCENT,
        max_width=rs2_w, line_height=20, start_size=16, min_size=10,
    )

    # Car stats (stream compositor shows the first car; no dropdown).
    cs_x = rs2_x + rs2_w + 10
    cs_w = bw - cs_x - 10
    if cars and cs_w > 100:
        selected = cars[0]
        cn = selected["car_number"]
        header_prefix = "Car #{}: ".format(cn) if cn > 0 else "Car: "
        header_name = selected["instance_name"][:16] if cn > 0 else selected["instance_name"][:18]
        draw_lines_fit_segmented(
            bottom_surface, [(header_prefix, header_name, "")], cs_x, ss1_y + 30,
            PANE_TEXT, CAR_NAME_ACCENT,
            max_width=cs_w, line_height=24, start_size=22, min_size=10,
        )
        car_lines = [
            "State: {}".format(selected["state"]),
            "Speed: {:.1f}".format(selected["speed"]),
            "Fuel: {:.1f}".format(selected["fuel"]),
            "Tire: {:.1f}".format(selected["tire_health"]),
            "Damage: {:.1f}".format(selected["damage"]),
            "Laps: {}".format(selected["laps"]),
            "Best: {:.2f}s".format(selected["best_lap_seconds"]) if selected["best_lap_seconds"] > 0 else "Best: --",
        ]
        draw_lines_fit(bottom_surface, car_lines, cs_x, ss1_y + 58, PANE_TEXT, max_width=cs_w, line_height=20, start_size=16, min_size=10)



class HudStateSender:
    """Fire-and-forget publisher of HUD snapshots to the compositor process.

    Sends JSON over a Unix datagram socket. Datagrams are non-blocking and dropped if the
    receiver isn't up yet, so a missing/lagged HUD process never stalls the renderer.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.setblocking(False)

    def send(self, snapshot: dict) -> None:
        try:
            data = json.dumps(snapshot).encode("utf-8")
            self._sock.sendto(data, self._path)
        except (OSError, ValueError):
            pass  # receiver absent / transient error -> drop

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def overlay_config(default_conf: Path) -> dict:
    """Load the overlay's configuration from the live tracksim.conf (capture + HUD sizes)."""
    return read_simple_conf(
        default_conf,
        {
            "window_width": "1280",
            "window_height": "720",
            "capture_width": "960",
            "capture_height": "540",
            "stream_leaderboard_width": "280",
            "stream_bottom_stats_height": "220",
            "stream_show_panes": "0",
            "series_logo": "",
            "tracks_dir": "tracks",
        },
    )


def hud_sizes(conf: dict, w: int, h: int):
    """Return (side_w, bottom_h) for the HUD panes, clamped like the renderer."""
    side_w = min(max(1, as_int(conf, "stream_leaderboard_width", 280)), w - 1)
    bottom_h = min(max(1, as_int(conf, "stream_bottom_stats_height", 220)), h - 1)
    return side_w, bottom_h
