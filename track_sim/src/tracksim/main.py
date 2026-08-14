from __future__ import annotations

import hashlib
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import pygame

from src.common.config import as_int, as_str, read_simple_conf
from src.common.geometry import point_in_polygon
from src.common.streamer import FrameStreamer, resolve_fifo_path
from src.common.io import load_car, load_track, save_track
from src.common.models import (
    VISION_X_BINS,
    VISION_Y_BINS,
    CarBehaviorProfile,
    CarConfig,
    CarLearningState,
    CarRaceMemory,
    CarRaceOutcome,
    CarRoutePlan,
    CarRuntimeState,
    CarSeriesStats,
    SeriesLapRecord,
    VisionMatrix,
    Waypoint,
)
from src.common.physics import _car_is_on_racing_surface, update_car_state
from src.common.ui import (
    create_default_font,
    draw_dropdown_menus,
    draw_file_picker,
    draw_lines,
    draw_lines_fit,
    menu_action_at,
    render_text_fit,
)

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional dependency fallback
    Image = None


@dataclass
class SimCar:
    instance_name: str
    source_file: str
    config: CarConfig
    state: CarRuntimeState
    start_pose: tuple[float, float, float]
    behavior: CarBehaviorProfile = field(default_factory=CarBehaviorProfile)
    learning: CarLearningState = field(default_factory=CarLearningState)
    memory: CarRaceMemory = field(default_factory=CarRaceMemory)
    race_elapsed: float = 0.0
    speed_accum: float = 0.0
    speed_samples: int = 0
    max_race_speed: float = 0.0
    preferred_line_offset: float = 0.0
    line_offset_frozen: bool = False
    barrier_hits: int = 0
    best_lap_seconds: float = 0.0
    last_lap_seconds: float = 0.0
    lap_start_time: float = 0.0
    last_lap_damage_checkpoint: float = 0.0
    pass_side_bias: float = 0.0
    pace_bias: float = 1.0
    steer_bias: float = 1.0
    route_plan: CarRoutePlan = field(default_factory=CarRoutePlan)
    vision_matrix: VisionMatrix = field(default_factory=VisionMatrix.empty)
    last_visible_line_point: tuple[float, float] | None = None
    last_damage_sample: float = 0.0
    route_last_idx: int = -1
    route_last_dist: float = float("inf")
    route_stall_time: float = 0.0
    route_stall_recover_time: float = 0.0
    route_idx_stall_time: float = 0.0
    post_waypoint_boost_time: float = 0.0
    waypoint_behind_time: float = 0.0
    hard_route_stall_time: float = 0.0
    hard_route_recenter_time: float = 0.0
    speed_flip_stall_time: float = 0.0
    last_speed_sign: int = 0
    # Series and race-stats-2 tracking.
    series_stats: CarSeriesStats = field(default_factory=CarSeriesStats)
    max_drift_duration: float = 0.0
    current_drift_start: float | None = None
    first_crash_time: float | None = None
    last_contact_time: float | None = None
    last_contact_partner: str | None = None
    race_number: int = 0
    car_number: int = 0
    completed_lap_limit: bool = False
    finish_time: float = 0.0


@dataclass
class RaceDecisionLogger:
    file_path: Path
    _last_by_car: dict[str, tuple[str, str]] = field(default_factory=dict)
    _events_by_car: dict[str, list[tuple[float, str, str, float]]] = field(default_factory=dict)
    _last_tick_time_by_car: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def _prune_old_logs(logs_dir: Path, keep: int) -> None:
        log_files = sorted(
            logs_dir.glob("race-*.log"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        overflow = len(log_files) - keep
        if overflow <= 0:
            return
        for old_path in log_files[:overflow]:
            try:
                old_path.unlink()
            except OSError:
                continue

    @classmethod
    def start(cls, logs_dir: Path, track_name: str, cars: list[SimCar]) -> "RaceDecisionLogger":
        logs_dir.mkdir(parents=True, exist_ok=True)
        cls._prune_old_logs(logs_dir, keep=9)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_track = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in track_name).strip("_") or "track"
        path = logs_dir / f"race-{stamp}-{safe_track}.log"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(f"race_start={datetime.now().isoformat(timespec='seconds')}\n")
            handle.write(f"track={track_name}\n")
            handle.write(f"cars={','.join(entry.instance_name for entry in cars)}\n")
        return cls(file_path=path)

    def log_decision(self, race_elapsed: float, car_name: str, mode: str, reason: str, speed: float, force: bool = False) -> None:
        key = (mode, reason)
        if not force and self._last_by_car.get(car_name) == key:
            return
        self._last_by_car[car_name] = key
        self._events_by_car.setdefault(car_name, []).append((race_elapsed, mode, reason, speed))
        with self.file_path.open("a", encoding="utf-8") as handle:
            handle.write(f"t={race_elapsed:.2f} car={car_name} mode={mode} reason={reason} speed={speed:.2f}\n")

    def log_tick(self, race_elapsed: float, car_name: str, speed: float, route_idx: int, vision_center: str) -> None:
        last_tick = self._last_tick_time_by_car.get(car_name, -1.0)
        if last_tick >= 0.0 and race_elapsed - last_tick < 0.5:
            return
        self._last_tick_time_by_car[car_name] = race_elapsed
        with self.file_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"t={race_elapsed:.2f} car={car_name} mode=tick reason=telemetry speed={speed:.2f} "
                f"route_idx={route_idx} vision_center={vision_center}\n"
            )

    def write_race_results(self, sim_cars: list[SimCar]) -> None:
        """Write a non-series race result block to the decision log."""
        with self.file_path.open("a", encoding="utf-8") as handle:
            handle.write("race_results_begin\n")
            for entry in sim_cars:
                handle.write(
                    f"car={entry.instance_name} number={entry.car_number} "
                    f"laps={entry.state.laps} completed_lap_limit={int(entry.completed_lap_limit)} "
                    f"finish_time={entry.finish_time:.2f} best_lap={entry.best_lap_seconds:.2f} "
                    f"crashed={int(entry.state.state == 'crashed')} points={entry.series_stats.points}\n"
                )
            handle.write("race_results_end\n")

    def write_summary(self) -> None:
        left_track_total = 0
        crashed_total = 0
        left_to_crash_3s_total = 0
        per_car_pairs: dict[str, int] = {}

        for car_name, events in self._events_by_car.items():
            car_pairs = 0
            for i, (event_t, mode, _reason, _speed) in enumerate(events):
                if mode == "left_track":
                    left_track_total += 1
                    crash_idx = next(
                        (
                            j
                            for j in range(i + 1, len(events))
                            if events[j][1] == "crashed" and events[j][0] - event_t <= 3.0
                        ),
                        None,
                    )
                    if crash_idx is not None:
                        car_pairs += 1
                elif mode == "crashed":
                    crashed_total += 1

            if car_pairs > 0:
                per_car_pairs[car_name] = car_pairs
            left_to_crash_3s_total += car_pairs

        with self.file_path.open("a", encoding="utf-8") as handle:
            handle.write("summary_begin\n")
            handle.write(f"summary_left_track_events={left_track_total}\n")
            handle.write(f"summary_crash_events={crashed_total}\n")
            handle.write(f"summary_left_track_to_crash_within_3s={left_to_crash_3s_total}\n")
            if per_car_pairs:
                pairs_text = ",".join(f"{name}:{count}" for name, count in sorted(per_car_pairs.items()))
                handle.write(f"summary_left_track_to_crash_within_3s_per_car={pairs_text}\n")
            handle.write("summary_end\n")


def load_latest(path: Path, suffix: str):
    files = sorted(path.glob(f"*{suffix}"))
    if not files:
        return None
    return files[-1]


def _personality_unit(seed_text: str, salt: str) -> float:
    digest = hashlib.sha256(f"{seed_text}|{salt}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return value / float((1 << 64) - 1)


def _build_personality(instance_name: str, car: CarConfig) -> tuple[CarBehaviorProfile, CarLearningState, float, float, float]:
    base = f"{instance_name}:{car.name}:{car.mass:.1f}:{car.max_speed:.1f}"

    def span(salt: str, low: float, high: float) -> float:
        t = _personality_unit(base, salt)
        return low + (high - low) * t

    profile = CarBehaviorProfile(
        speed_priority=span("speed_priority", 0.85, 1.25),
        lap_improvement_priority=span("lap_priority", 0.75, 1.2),
        damage_avoidance_priority=span("damage_avoid", 0.55, 1.05),
        keep_nose_forward_priority=span("nose_forward", 0.55, 1.1),
        avoid_slowdown_priority=span("avoid_slow", 0.65, 1.15),
        barrier_avoidance_priority=span("barrier_avoid", 0.55, 1.15),
        risk_tolerance=span("risk", 0.35, 0.9),
    )

    learning = CarLearningState(
        target_speed_bias=span("target_speed_bias", 0.9, 1.18),
        steering_aggression=span("steer_aggression", 0.82, 1.18),
        safety_bias=span("safety_bias", 0.86, 1.18),
    )

    pass_side_bias = span("pass_side", -1.0, 1.0)
    pace_bias = span("pace_bias", 0.9, 1.13)
    steer_bias = span("steer_bias", 0.9, 1.12)
    return profile, learning, pass_side_bias, pace_bias, steer_bias


def _ccw_spawn_heading(centerline: list[tuple[float, float]], index: int) -> float:
    if len(centerline) < 2:
        return -math.pi / 2

    cx = sum(p[0] for p in centerline) / len(centerline)
    cy = sum(p[1] for p in centerline) / len(centerline)

    curr = centerline[index]
    prev_pt = centerline[(index - 1) % len(centerline)]
    next_pt = centerline[(index + 1) % len(centerline)]

    candidates = [
        (next_pt[0] - curr[0], next_pt[1] - curr[1]),
        (prev_pt[0] - curr[0], prev_pt[1] - curr[1]),
    ]

    rx = curr[0] - cx
    ry = curr[1] - cy
    best = candidates[0]
    best_cross = float("inf")
    for tx, ty in candidates:
        cross = rx * ty - ry * tx
        if cross < best_cross:
            best_cross = cross
            best = (tx, ty)

    return math.atan2(best[1], best[0])


def spawn_state(track, car: CarConfig) -> CarRuntimeState:
    x, y, w, h = track.start_grid
    spawn_x = x + w / 2
    spawn_y = y + h / 2

    centerline = [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(min(len(track.outer_points), len(track.inner_points)))
    ]

    heading = -math.pi / 2
    if len(centerline) >= 2:
        nearest_index = 0
        nearest_dist = float("inf")
        for i, pt in enumerate(centerline):
            dx = pt[0] - spawn_x
            dy = pt[1] - spawn_y
            dist = dx * dx + dy * dy
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_index = i
        heading = _ccw_spawn_heading(centerline, nearest_index)

    return CarRuntimeState(
        x=spawn_x,
        y=spawn_y,
        heading_radians=heading,
        speed=0.0,
        tire_health=car.starting_tire_health,
        fuel=car.starting_fuel,
        damage=0.0,
        last_x=spawn_x,
        last_y=spawn_y,
    )


def _smooth_loop(points: list[tuple[float, float]], piece_types: list[str]) -> list[tuple[float, float]]:
    if len(points) < 4 or len(piece_types) != len(points):
        return points

    out: list[tuple[float, float]] = []
    corner_ratio = 0.33
    for i, curr in enumerate(points):
        prev = points[(i - 1) % len(points)]
        nxt = points[(i + 1) % len(points)]
        if piece_types[i] != "curve":
            out.append(curr)
            continue

        entry = (
            curr[0] + (prev[0] - curr[0]) * corner_ratio,
            curr[1] + (prev[1] - curr[1]) * corner_ratio,
        )
        exit = (
            curr[0] + (nxt[0] - curr[0]) * corner_ratio,
            curr[1] + (nxt[1] - curr[1]) * corner_ratio,
        )
        out.append(entry)
        for step in range(1, 7):
            t = step / 7.0
            omt = 1.0 - t
            bezier = (
                omt * omt * entry[0] + 2 * omt * t * curr[0] + t * t * exit[0],
                omt * omt * entry[1] + 2 * omt * t * curr[1] + t * t * exit[1],
            )
            out.append(bezier)
        out.append(exit)
    return out


def draw_track(surface: pygame.Surface, track, transform: tuple[float, float, float] | None = None) -> None:
    outer_types = [piece.piece_type for piece in track.outer_pieces]
    inner_types = [piece.piece_type for piece in track.inner_pieces]
    outer_path = _smooth_loop(track.outer_points, outer_types)
    inner_path = _smooth_loop(track.inner_points, inner_types)
    if transform is not None:
        outer_path = [_to_screen(pt, transform) for pt in outer_path]
        inner_path = [_to_screen(pt, transform) for pt in inner_path]
        sg_rect = _to_screen_rect(track.start_grid, transform)
    else:
        sg_rect = pygame.Rect(track.start_grid)

    pygame.draw.polygon(surface, (10, 10, 10), outer_path)
    pygame.draw.polygon(surface, (42, 145, 75), inner_path)
    pygame.draw.lines(surface, (220, 220, 220), True, outer_path, 3)
    pygame.draw.lines(surface, (220, 220, 220), True, inner_path, 3)
    pygame.draw.rect(surface, (230, 190, 40), sg_rect)


def _compute_track_view_transform(track, pane: pygame.Rect, padding: int = 20) -> tuple[float, float, float]:
    """Return (scale, offset_x, offset_y) to map track coords into the pane."""
    all_x = [p[0] for p in track.outer_points] + [p[0] for p in track.inner_points]
    all_y = [p[1] for p in track.outer_points] + [p[1] for p in track.inner_points]
    sg_x, sg_y, sg_w, sg_h = track.start_grid
    all_x.extend([sg_x, sg_x + sg_w])
    all_y.extend([sg_y, sg_y + sg_h])
    if not all_x or not all_y:
        return (1.0, 0.0, 0.0)
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    track_w = max_x - min_x
    track_h = max_y - min_y
    if track_w <= 0 or track_h <= 0:
        return (1.0, 0.0, 0.0)
    area_w = max(1, pane.width - 2 * padding)
    area_h = max(1, pane.height - 2 * padding)
    scale = min(area_w / track_w, area_h / track_h)
    offset_x = pane.x + padding + (area_w - track_w * scale) * 0.5 - min_x * scale
    offset_y = pane.y + padding + (area_h - track_h * scale) * 0.5 - min_y * scale
    return (scale, offset_x, offset_y)


def _to_screen(pt: tuple[float, float], transform: tuple[float, float, float]) -> tuple[int, int]:
    """Convert a track-coordinate point to screen coordinates."""
    scale, ox, oy = transform
    return (int(pt[0] * scale + ox), int(pt[1] * scale + oy))


def _to_screen_rect(rect_tuple: tuple[float, float, float, float], transform: tuple[float, float, float]) -> pygame.Rect:
    """Convert a track-coordinate rect to a screen-coordinate pygame.Rect."""
    scale, ox, oy = transform
    x, y, w, h = rect_tuple
    sx = int(x * scale + ox)
    sy = int(y * scale + oy)
    sw = max(1, int(w * scale))
    sh = max(1, int(h * scale))
    return pygame.Rect(sx, sy, sw, sh)


def _from_screen(sx: float, sy: float, transform: tuple[float, float, float]) -> tuple[float, float]:
    """Convert screen coordinates back to track coordinates."""
    scale, ox, oy = transform
    if scale <= 0:
        return (sx, sy)
    return ((sx - ox) / scale, (sy - oy) / scale)


def draw_car(surface: pygame.Surface, state: CarRuntimeState, car: CarConfig, transform: tuple[float, float, float] | None = None) -> None:
    if transform is not None:
        sx, sy = _to_screen((state.x, state.y), transform)
        scaled_len = int(car.length * transform[0])
        scaled_wid = int(car.width * transform[0])
    else:
        sx, sy = int(state.x), int(state.y)
        scaled_len = int(car.length)
        scaled_wid = int(car.width)
    body = pygame.Surface((max(1, scaled_len), max(1, scaled_wid)), pygame.SRCALPHA)
    body.fill(car.body_color)
    nose = pygame.Rect(int(scaled_len * 0.7), 0, int(scaled_len * 0.3), scaled_wid)
    pygame.draw.rect(body, car.nose_color, nose)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    rect = rotated.get_rect(center=(sx, sy))
    surface.blit(rotated, rect)


def _car_draw_rect(state: CarRuntimeState, car: CarConfig, transform: tuple[float, float, float] | None = None) -> pygame.Rect:
    if transform is not None:
        sx, sy = _to_screen((state.x, state.y), transform)
        scaled_len = int(car.length * transform[0])
        scaled_wid = int(car.width * transform[0])
    else:
        sx, sy = int(state.x), int(state.y)
        scaled_len = int(car.length)
        scaled_wid = int(car.width)
    body = pygame.Surface((max(1, scaled_len), max(1, scaled_wid)), pygame.SRCALPHA)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    return rotated.get_rect(center=(sx, sy))


def _seed_route_target_from_pose(
    route_plan: CarRoutePlan,
    pose: tuple[float, float, float],
    start_grid: tuple[float, float, float, float] | None = None,
) -> None:
    waypoints = route_plan.permanent_waypoints
    if not waypoints:
        route_plan.active_target_index = 0
        return

    x, y, heading = pose
    fwd_x = math.cos(heading)
    fwd_y = math.sin(heading)

    nearest_idx = 0
    nearest_dist_sq = float("inf")
    for idx, wp in enumerate(waypoints):
        dx = wp.x - x
        dy = wp.y - y
        dist_sq = dx * dx + dy * dy
        if dist_sq < nearest_dist_sq:
            nearest_dist_sq = dist_sq
            nearest_idx = idx

    n = len(waypoints)

    next_idx = (nearest_idx + 1) % n
    prev_idx = (nearest_idx - 1) % n
    next_vec = (waypoints[next_idx].x - waypoints[nearest_idx].x, waypoints[next_idx].y - waypoints[nearest_idx].y)
    prev_vec = (waypoints[prev_idx].x - waypoints[nearest_idx].x, waypoints[prev_idx].y - waypoints[nearest_idx].y)
    dot_next = next_vec[0] * fwd_x + next_vec[1] * fwd_y
    dot_prev = prev_vec[0] * fwd_x + prev_vec[1] * fwd_y
    step_dir = 1 if dot_next >= dot_prev else -1

    if step_dir < 0:
        # Route progression always advances +1 index. If heading implies reverse
        # travel, reverse waypoint storage so progression order matches heading.
        route_plan.permanent_waypoints = list(reversed(route_plan.permanent_waypoints))
        waypoints = route_plan.permanent_waypoints
        n = len(waypoints)
        nearest_idx = (n - 1) - nearest_idx
        step_dir = 1

    start_rect = pygame.Rect(start_grid) if start_grid is not None else None

    for step in range(1, n + 1):
        idx = (nearest_idx + step_dir * step) % n
        wp = waypoints[idx]

        if start_rect is not None and start_rect.collidepoint(wp.x, wp.y):
            continue

        dx = wp.x - x
        dy = wp.y - y
        forward = dx * fwd_x + dy * fwd_y
        if forward > 1.0:
            route_plan.active_target_index = idx
            return

    route_plan.active_target_index = (nearest_idx + step_dir) % n


def _reset_for_race(sim_car: SimCar, track) -> None:
    state = sim_car.state
    car = sim_car.config
    state.x, state.y, state.heading_radians = sim_car.start_pose
    state.speed = 0.0
    state.vx = 0.0
    state.vy = 0.0
    state.yaw_rate = 0.0
    state.tire_health = car.starting_tire_health
    state.fuel = car.starting_fuel
    state.damage = 0.0
    state.state = "stopped"
    state.laps = 0
    state.left_start_zone = False
    state.cumulative_angle = 0.0
    state.nav_direction = 0
    state.nav_last_index = -1
    state.nav_stall_frames = 0
    state.wall_contact_frames = 0
    state.distance_traveled = 0.0
    state.last_lap_distance = 0.0
    state.last_x = state.x
    state.last_y = state.y

    sim_car.race_elapsed = 0.0
    sim_car.speed_accum = 0.0
    sim_car.speed_samples = 0
    sim_car.max_race_speed = 0.0
    sim_car.completed_lap_limit = False
    sim_car.finish_time = 0.0
    baseline_offset = car.width * 0.25 * car.line_offset_scale
    if baseline_offset < 1.0:
        baseline_offset = 0.0
    sim_car.preferred_line_offset = baseline_offset if sim_car.pass_side_bias >= 0.0 else -baseline_offset
    sim_car.line_offset_frozen = False
    sim_car.barrier_hits = 0
    sim_car.best_lap_seconds = 0.0
    sim_car.last_lap_seconds = 0.0
    sim_car.lap_start_time = 0.0
    sim_car.last_lap_damage_checkpoint = 0.0
    sim_car.last_visible_line_point = None
    _rebuild_route_for_pose(track, sim_car.route_plan, sim_car.start_pose)
    sim_car.route_last_idx = sim_car.route_plan.active_target_index
    sim_car.route_last_dist = float("inf")
    sim_car.route_stall_time = 0.0
    sim_car.route_stall_recover_time = 0.0
    sim_car.route_idx_stall_time = 0.0
    sim_car.post_waypoint_boost_time = 0.0
    sim_car.waypoint_behind_time = 0.0
    sim_car.hard_route_stall_time = 0.0
    sim_car.hard_route_recenter_time = 0.0
    sim_car.speed_flip_stall_time = 0.0
    sim_car.last_speed_sign = 0


def _increase_permanent_waypoints(track, route_plan: CarRoutePlan, increment: int = 3) -> None:
    if increment <= 0:
        return

    centerline = _build_centerline(track)
    n = len(centerline)
    if n < 4:
        return

    old_active = route_plan.active_waypoint()
    current_count = max(4, len(route_plan.permanent_waypoints))
    target_count = min(n, current_count + increment)
    if target_count <= len(route_plan.permanent_waypoints):
        return

    points = _resample_centerline_points(centerline, target_count=target_count, straight_bias=2.1, turn_floor=0.4)
    if len(points) < 4:
        points = centerline
    new_waypoints = [
        Waypoint(x=pt[0], y=pt[1], kind="permanent", source="generated")
        for pt in points
    ]
    route_plan.permanent_waypoints = new_waypoints

    if old_active is None:
        route_plan.active_target_index = 0
        return

    best_idx = 0
    best_dist_sq = float("inf")
    for idx, wp in enumerate(route_plan.permanent_waypoints):
        dx = wp.x - old_active.x
        dy = wp.y - old_active.y
        dist_sq = dx * dx + dy * dy
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_idx = idx
    route_plan.active_target_index = best_idx


def _decrease_permanent_waypoints(track, route_plan: CarRoutePlan, decrement: int = 3) -> None:
    if decrement <= 0:
        return

    centerline = _build_centerline(track)
    n = len(centerline)
    if n < 4:
        return

    old_active = route_plan.active_waypoint()
    current_count = max(4, len(route_plan.permanent_waypoints))
    target_count = max(4, current_count - decrement)
    if target_count >= len(route_plan.permanent_waypoints):
        return

    points = _resample_centerline_points(centerline, target_count=target_count, straight_bias=2.1, turn_floor=0.4)
    if len(points) < 4:
        points = centerline[:4]
    new_waypoints = [
        Waypoint(x=pt[0], y=pt[1], kind="permanent", source="generated")
        for pt in points
    ]
    route_plan.permanent_waypoints = new_waypoints

    if old_active is None:
        route_plan.active_target_index = 0
        return

    best_idx = 0
    best_dist_sq = float("inf")
    for idx, wp in enumerate(route_plan.permanent_waypoints):
        dx = wp.x - old_active.x
        dy = wp.y - old_active.y
        dist_sq = dx * dx + dy * dy
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_idx = idx
    route_plan.active_target_index = best_idx


def draw_crash_fallback(surface: pygame.Surface, state: CarRuntimeState, car: CarConfig) -> None:
    cx = int(state.x)
    cy = int(state.y)
    w = max(12, int(car.length * 0.35))
    h = max(16, int(car.width * 1.4))
    flame_points = [
        (cx, cy - h),
        (cx - w, cy + h // 3),
        (cx - w // 3, cy + h),
        (cx + w // 3, cy + h),
        (cx + w, cy + h // 3),
    ]
    core_points = [
        (cx, cy - int(h * 0.55)),
        (cx - int(w * 0.45), cy + int(h * 0.2)),
        (cx, cy + int(h * 0.65)),
        (cx + int(w * 0.45), cy + int(h * 0.2)),
    ]
    pygame.draw.polygon(surface, (255, 120, 35, 230), flame_points)
    pygame.draw.polygon(surface, (255, 220, 90, 230), core_points)


def _load_crash_overlay(project_dir: Path) -> pygame.Surface | None:
    def _load_with_pillow(path: Path) -> pygame.Surface | None:
        if Image is None:
            return None
        try:
            image = Image.open(path).convert("RGBA")
            data = image.tobytes()
            return pygame.image.fromstring(data, image.size, "RGBA").convert_alpha()
        except Exception:
            return None

    image_dir = project_dir / "images"
    for name in ("flame_affect_car.png", "flame_effect_car.png"):
        path = image_dir / name
        if path.exists():
            try:
                return pygame.image.load(path.as_posix()).convert_alpha()
            except pygame.error:
                pillow_surface = _load_with_pillow(path)
                if pillow_surface is not None:
                    return pillow_surface

                try:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    subprocess.run(
                        ["sips", "-s", "format", "png", path.as_posix(), "--out", tmp_path.as_posix()],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        pygame_surface = pygame.image.load(tmp_path.as_posix()).convert_alpha()
                        return pygame_surface
                    except pygame.error:
                        return _load_with_pillow(tmp_path)
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
    return None


def _load_series_logo(project_dir: Path, logo_path: str) -> pygame.Surface | None:
    """Load a series logo image, returning None if missing or invalid."""
    if not logo_path:
        return None
    candidates: list[Path] = []
    raw = Path(logo_path)
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(project_dir / "images" / "logos" / raw)
        candidates.append(project_dir / raw)
    for path in candidates:
        if not path.exists():
            continue
        try:
            return pygame.image.load(path.as_posix()).convert_alpha()
        except pygame.error:
            if Image is not None:
                try:
                    image = Image.open(path).convert("RGBA")
                    data = image.tobytes()
                    return pygame.image.fromstring(data, image.size, "RGBA").convert_alpha()
                except Exception:
                    return None
            return None
    return None


def _stream_chrome_signature(sim_cars, series_name, series_completed_races, series_race_target) -> str:
    """Compact string capturing everything the stream chrome displays.

    It is cheap to rebuild this each frame, and it's used to detect when the cached
    chrome overlay actually needs re-rendering instead of just blitting the cached copy.
    """
    parts: list[str] = [f"{series_name}|{series_completed_races}|{series_race_target}"]
    for e in sorted(sim_cars, key=lambda e: (e.state.laps, e.state.distance_traveled), reverse=True):
        parts.append(
            f"{e.car_number}|{e.instance_name}|{e.state.laps}|{round(e.state.distance_traveled, 0)}|"
            f"{e.series_stats.points}|{round(e.max_race_speed, 1)}|{round(e.best_lap_seconds, 2)}"
        )
    return "\n".join(parts)


def _render_stream_chrome(
    lb_surface,
    bottom_surface,
    sim_cars,
    font,
    series_name,
    series_completed_races,
    series_race_target,
    series_logo_scaled,
) -> None:
    """Fill and draw the leaderboard + bottom-stats panes into their own OPAQUE cached
    surfaces. Each surface is pane-sized and is blitted beside the track (the track pane
    is already shrunk to leave room), so the chrome never overlaps the race. Re-rendered
    only when the standings change."""
    # Small header font for the narrow/short HUD panes (the main `font` is 22pt,
    # far too large for the bottom strip at capture size).
    hfont = create_default_font(12)
    # --- Leaderboard pane ---
    lb_surface.fill((24, 28, 34))
    pygame.draw.rect(lb_surface, (60, 70, 86), lb_surface.get_rect(), width=1)
    draw_lines(lb_surface, hfont, ["Leaderboard"], 8, 6, (235, 235, 235))
    lb_y = 34
    ordered = sorted(sim_cars, key=lambda e: (e.state.laps, e.state.distance_traveled), reverse=True)
    for pos, entry in enumerate(ordered[:12], start=1):
        cn = entry.car_number
        label = f"#{cn} {entry.instance_name[:16]}" if cn > 0 else entry.instance_name[:18]
        draw_lines_fit(
            lb_surface,
            [f"{pos}. {label}  L{entry.state.laps}  {entry.series_stats.points}p"],
            6,
            lb_y,
            (235, 235, 235),
            max_width=lb_surface.get_width() - 12,
            line_height=20,
            start_size=15,
            min_size=9,
        )
        lb_y += 20

    # --- Bottom stats pane ---
    bw = bottom_surface.get_width()
    bottom_surface.fill((24, 28, 34))
    pygame.draw.rect(bottom_surface, (60, 70, 86), bottom_surface.get_rect(), width=1)

    c1_x = 8
    c1_w = max(80, int(bw * 0.32))
    c2_x = c1_x + c1_w + 8
    c2_w = max(90, int(bw * 0.30))
    c3_x = c2_x + c2_w + 8
    c3_w = max(80, bw - c3_x - 8)

    rtgt = series_race_target if series_race_target > 0 else "∞"

    # Series stats
    draw_lines(bottom_surface, hfont, ["Series"], c1_x, 4, (200, 220, 255))
    ser_lines = [f"{series_name}  R{series_completed_races}/{rtgt}" if series_name
                 else f"R{series_completed_races}/{rtgt}"]
    all_fastest = [e.series_stats.fastest_lap for e in sim_cars if e.series_stats.fastest_lap]
    if all_fastest:
        fr = min(all_fastest, key=lambda r: r.lap_time)
        ser_lines.append(f"Fast {fr.lap_time:.2f}s {fr.car_name[:10]}")
    draw_lines_fit(bottom_surface, ser_lines, c1_x, 22, (225, 225, 225),
                   max_width=c1_w, line_height=16, start_size=12, min_size=8)

    # Race stats
    draw_lines(bottom_surface, hfont, ["Race"], c2_x, 4, (200, 220, 255))
    race_lines: list[str] = []
    if sim_cars:
        leader = max(sim_cars, key=lambda e: (e.state.laps, e.state.distance_traveled))
        race_lines.append(f"Leader {leader.instance_name[:12]} L{leader.state.laps}")
    top_speed = max((e.max_race_speed for e in sim_cars), default=0.0)
    race_lines.append(f"Top {top_speed:.0f}")
    draw_lines_fit(bottom_surface, race_lines, c2_x, 22, (225, 225, 225),
                   max_width=c2_w, line_height=16, start_size=12, min_size=8)

    # Points leaders
    draw_lines(bottom_surface, hfont, ["Points"], c3_x, 4, (200, 220, 255))
    _leaders = sorted(sim_cars, key=lambda e: e.series_stats.points, reverse=True)[:3]
    pt_lines = [f"{i}. {e.instance_name[:10]} {e.series_stats.points}p" for i, e in enumerate(_leaders, start=1)]
    draw_lines_fit(bottom_surface, pt_lines, c3_x, 22, (225, 225, 225),
                   max_width=c3_w, line_height=16, start_size=12, min_size=8)

    # Series logo (bottom-right of the strip), if configured.
    if series_logo_scaled is not None:
        bottom_surface.blit(series_logo_scaled, (bw - 84, 6))



def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _build_centerline(track) -> list[tuple[float, float]]:
    if track._centerline_smooth is not None:
        return track._centerline_smooth
    outer_types = [piece.piece_type for piece in track.outer_pieces]
    inner_types = [piece.piece_type for piece in track.inner_pieces]
    outer_path = _smooth_loop(track.outer_points, outer_types)
    inner_path = _smooth_loop(track.inner_points, inner_types)

    count = min(len(outer_path), len(inner_path))
    base = [
        (
            (outer_path[i][0] + inner_path[i][0]) * 0.5,
            (outer_path[i][1] + inner_path[i][1]) * 0.5,
        )
        for i in range(count)
    ]
    if len(base) < 2:
        return base

    dense: list[tuple[float, float]] = []
    subdivisions = 8
    for i in range(len(base)):
        a = base[i]
        b = base[(i + 1) % len(base)]
        for step in range(subdivisions):
            t = step / subdivisions
            dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    track._centerline_smooth = dense
    return dense


def _resample_centerline_points(
    centerline: list[tuple[float, float]],
    target_count: int,
    straight_bias: float = 2.1,
    turn_floor: float = 0.4,
) -> list[tuple[float, float]]:
    n = len(centerline)
    if n <= 1 or target_count <= 0:
        return []

    def _curvature_at(idx: int) -> float:
        prev_pt = centerline[(idx - 1) % n]
        curr_pt = centerline[idx]
        next_pt = centerline[(idx + 1) % n]
        in_angle = math.atan2(curr_pt[1] - prev_pt[1], curr_pt[0] - prev_pt[0])
        out_angle = math.atan2(next_pt[1] - curr_pt[1], next_pt[0] - curr_pt[0])
        return abs(_wrap_angle(out_angle - in_angle))

    curvatures = [_curvature_at(i) for i in range(n)]
    max_curv = max(curvatures) if curvatures else 0.0
    if max_curv <= 1e-9:
        weights = [1.0 for _ in range(n)]
    else:
        weights = []
        for c in curvatures:
            norm = max(0.0, min(1.0, c / max_curv))
            # Lower weight in turns and higher weight in straights.
            weights.append(turn_floor + (1.0 - norm) * straight_bias)

    seg_weighted_lengths: list[float] = []
    cumulative: list[float] = []
    total = 0.0
    for i in range(n):
        a = centerline[i]
        b = centerline[(i + 1) % n]
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        seg_weight = (weights[i] + weights[(i + 1) % n]) * 0.5
        weighted_len = max(1e-6, seg_len * seg_weight)
        seg_weighted_lengths.append(weighted_len)
        total += weighted_len
        cumulative.append(total)

    points: list[tuple[float, float]] = []
    seg_idx = 0
    for k in range(target_count):
        target = (k * total) / max(1, target_count)
        while seg_idx < n - 1 and cumulative[seg_idx] < target:
            seg_idx += 1
        prev_total = cumulative[seg_idx - 1] if seg_idx > 0 else 0.0
        seg_len = seg_weighted_lengths[seg_idx]
        t = (target - prev_total) / max(seg_len, 1e-6)
        t = max(0.0, min(1.0, t))
        ax, ay = centerline[seg_idx]
        bx, by = centerline[(seg_idx + 1) % n]
        points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return points


def _build_default_route_plan(track) -> CarRoutePlan:
    centerline = _build_centerline(track)
    n = len(centerline)
    if n <= 0:
        return CarRoutePlan()

    target_count = max(20, min(60, n // 3))
    points = _resample_centerline_points(centerline, target_count=target_count, straight_bias=2.1, turn_floor=0.4)
    if len(points) < 4:
        points = centerline

    waypoints = [
        Waypoint(x=pt[0], y=pt[1], kind="permanent", source="generated")
        for pt in points
    ]
    return CarRoutePlan(permanent_waypoints=waypoints)


def _pose_lateral_offset(centerline: list[tuple[float, float]], pose: tuple[float, float, float]) -> float:
    """Signed lateral distance (px) from a pose to the track centerline.

    Positive means the pose sits to the left of the forward centerline
    direction. Clamped so a position-derived route stays near the racing
    surface instead of wandering far off-line when a car is placed at an
    extreme lateral offset.
    """
    if len(centerline) < 2:
        return 0.0
    x, y, _heading = pose
    idx = _nearest_centerline_index(centerline, x, y)
    pt = centerline[idx]
    n = len(centerline)
    next_pt = centerline[(idx + 1) % n]
    tan_x = next_pt[0] - pt[0]
    tan_y = next_pt[1] - pt[1]
    tan_len = math.hypot(tan_x, tan_y)
    if tan_len <= 1e-6:
        return 0.0
    tan_x /= tan_len
    tan_y /= tan_len
    # Cross product: positive when the pose is to the left of the forward tangent.
    from_x = x - pt[0]
    from_y = y - pt[1]
    signed = tan_x * from_y - tan_y * from_x
    return max(-22.0, min(22.0, signed))


def _apply_pose_offset_to_route(track, route_plan: CarRoutePlan, pose: tuple[float, float, float]) -> bool:
    """Offset the route's existing waypoints to follow the racing line the car
    actually sits on (position-derived lateral offset), then normalize the
    order and re-seed the active target from the pose. Used after a resample
    (e.g. + Waypoint / - Waypoint) so the position-derived line is preserved.
    """
    waypoints = route_plan.permanent_waypoints
    if len(waypoints) < 3:
        return False

    centerline = _build_centerline(track)
    offset = _pose_lateral_offset(centerline, pose) if centerline else 0.0
    if abs(offset) >= 1e-4:
        m = len(waypoints)
        shifted: list[tuple[float, float]] = []
        for i, wp in enumerate(waypoints):
            prev_wp = waypoints[(i - 1) % m]
            next_wp = waypoints[(i + 1) % m]
            tan_x = next_wp.x - prev_wp.x
            tan_y = next_wp.y - prev_wp.y
            tan_len = math.hypot(tan_x, tan_y)
            if tan_len <= 1e-6:
                shifted.append((wp.x, wp.y))
                continue
            normal_x = -(tan_y / tan_len)
            normal_y = tan_x / tan_len
            shifted.append((wp.x + normal_x * offset, wp.y + normal_y * offset))
        route_plan.permanent_waypoints = [
            Waypoint(x=px, y=py, kind="permanent", source="generated")
            for px, py in shifted
        ]

    _normalize_route_order(track, route_plan)
    _seed_route_target_from_pose(route_plan, pose, track.start_grid)
    return True


def _rebuild_route_for_pose(track, route_plan: CarRoutePlan, pose: tuple[float, float, float]) -> bool:
    """Regenerate a car's waypoint path from a given pose.

    The route is resampled from the track centerline and offset laterally to
    follow the racing line the car actually sits on (a position-derived
    offset). This makes moving a car to a new position and resetting/starting
    actually recalculate the car's waypoints instead of keeping the stale path.
    """
    centerline = _build_centerline(track)
    n = len(centerline)
    if n <= 0:
        return False

    target_count = max(20, min(60, n // 3))
    points = _resample_centerline_points(centerline, target_count=target_count, straight_bias=2.1, turn_floor=0.4)
    if len(points) < 4:
        points = centerline

    route_plan.permanent_waypoints = [
        Waypoint(x=pt[0], y=pt[1], kind="permanent", source="generated")
        for pt in points
    ]

    return _apply_pose_offset_to_route(track, route_plan, pose)



def _nearest_centerline_index(centerline: list[tuple[float, float]], x: float, y: float) -> int:
    if not centerline:
        return 0
    best_idx = 0
    best_dist = float("inf")
    for idx, pt in enumerate(centerline):
        dx = pt[0] - x
        dy = pt[1] - y
        d = dx * dx + dy * dy
        if d < best_dist:
            best_dist = d
            best_idx = idx
    return best_idx


def _normalize_route_order(track, route_plan: CarRoutePlan) -> None:
    if len(route_plan.permanent_waypoints) < 3:
        return

    centerline = _build_centerline(track)
    if len(centerline) < 3:
        return

    active_wp = None
    if route_plan.permanent_waypoints:
        active_wp = route_plan.permanent_waypoints[route_plan.active_target_index % len(route_plan.permanent_waypoints)]

    ordered = sorted(
        route_plan.permanent_waypoints,
        key=lambda wp: _nearest_centerline_index(centerline, wp.x, wp.y),
    )
    route_plan.permanent_waypoints = ordered

    if active_wp is None:
        route_plan.active_target_index = 0
        return

    try:
        route_plan.active_target_index = route_plan.permanent_waypoints.index(active_wp)
    except ValueError:
        route_plan.active_target_index = min(route_plan.active_target_index, len(route_plan.permanent_waypoints) - 1)


def _load_route_plan_from_track(track, instance_name: str) -> CarRoutePlan:
    metadata = track.metadata if isinstance(track.metadata, dict) else {}
    raw_routes = metadata.get("car_routes", {})
    if isinstance(raw_routes, dict):
        raw_entry = raw_routes.get(instance_name)
        raw_list = raw_entry
        active_index = 0
        if isinstance(raw_entry, dict):
            raw_list = raw_entry.get("waypoints")
            try:
                active_index = int(raw_entry.get("active_target_index", 0))
            except (TypeError, ValueError):
                active_index = 0
        if isinstance(raw_list, list) and raw_list:
            waypoints = [Waypoint.from_dict(item) for item in raw_list if isinstance(item, dict)]
            if waypoints:
                loaded = CarRoutePlan(permanent_waypoints=waypoints, active_target_index=max(0, active_index))

                # Auto-upgrade legacy/generated routes so new corner-center waypoint
                # generation is applied even when old metadata exists.
                all_generated = all(wp.source == "generated" for wp in loaded.permanent_waypoints)
                if all_generated:
                    upgraded = _build_default_route_plan(track)
                    if upgraded.permanent_waypoints:
                        _normalize_route_order(track, upgraded)
                        return upgraded
                _normalize_route_order(track, loaded)
                return loaded
    fallback = _build_default_route_plan(track)
    _normalize_route_order(track, fallback)
    return fallback


def _serialize_car_routes(sim_cars: list[SimCar]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for entry in sim_cars:
        out[entry.instance_name] = {
            "active_target_index": entry.route_plan.active_target_index,
            "waypoints": [wp.to_dict() for wp in entry.route_plan.permanent_waypoints],
        }
    return out


def _serialize_car_entries(sim_cars: list[SimCar]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for entry in sim_cars:
        out[str(entry.car_number)] = {
            "name": entry.instance_name,
            "body_color": list(entry.config.body_color),
            "nose_color": list(entry.config.nose_color),
        }
    return out


def _draw_selected_car_overlays(screen: pygame.Surface, sim_car: SimCar, transform: tuple[float, float, float] | None = None) -> None:
    state = sim_car.state
    car = sim_car.config
    route = sim_car.route_plan

    def _to_screen_if(pt: tuple[float, float]) -> tuple[int, int]:
        if transform is not None:
            return _to_screen(pt, transform)
        return (int(pt[0]), int(pt[1]))

    def _offset_loop(points: list[tuple[float, float]], offset: float) -> list[tuple[float, float]]:
        if len(points) < 3 or abs(offset) < 1e-4:
            return points
        out: list[tuple[float, float]] = []
        n = len(points)
        for i in range(n):
            prev_pt = points[(i - 1) % n]
            next_pt = points[(i + 1) % n]
            tan_x = next_pt[0] - prev_pt[0]
            tan_y = next_pt[1] - prev_pt[1]
            tan_len = math.hypot(tan_x, tan_y)
            if tan_len <= 1e-6:
                out.append(points[i])
                continue
            tan_x /= tan_len
            tan_y /= tan_len
            normal_x = -tan_y
            normal_y = tan_x
            out.append((points[i][0] + normal_x * offset, points[i][1] + normal_y * offset))
        return out

    # Vision cone overlay (70 degrees, configured view distance).
    cone_range = car.effective_view_distance
    half_angle = math.radians(35.0)
    points = [(state.x, state.y)]
    steps = 12
    for i in range(steps + 1):
        t = i / steps
        angle = state.heading_radians - half_angle + (2.0 * half_angle * t)
        points.append((state.x + math.cos(angle) * cone_range, state.y + math.sin(angle) * cone_range))
    if transform is not None:
        points = [_to_screen(pt, transform) for pt in points]
    cone = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
    pygame.draw.polygon(cone, (110, 190, 255, 44), points)
    pygame.draw.lines(cone, (130, 220, 255, 140), False, points[1:], width=2)
    screen.blit(cone, (0, 0))

    if route.permanent_waypoints:
        pts = [(wp.x, wp.y) for wp in route.permanent_waypoints]
        screen_pts = [_to_screen_if(p) for p in pts]
        if len(screen_pts) > 1:
            pygame.draw.lines(screen, (70, 210, 120), True, screen_pts, width=2)
            preferred_pts = _offset_loop(pts, sim_car.preferred_line_offset)
            preferred_screen = [_to_screen_if(p) for p in preferred_pts]
            if len(preferred_screen) > 1:
                pygame.draw.lines(screen, (255, 150, 70), True, preferred_screen, width=2)
        for idx, wp in enumerate(route.permanent_waypoints):
            color = (255, 230, 130) if idx == route.active_target_index % max(1, len(route.permanent_waypoints)) else (70, 210, 120)
            pygame.draw.circle(screen, color, _to_screen_if((wp.x, wp.y)), 5)

def _best_worst_lap(sim_cars: list[SimCar]) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    laps = [(entry.instance_name, entry.best_lap_seconds) for entry in sim_cars if entry.best_lap_seconds > 0.0]
    if not laps:
        return None, None
    best = min(laps, key=lambda item: item[1])
    worst = max(laps, key=lambda item: item[1])
    return best, worst


def _vision_assign(matrix: VisionMatrix, x_bin: str, y_bin: str, state: str, distance: float) -> None:
    priority = {"clear": 0, "waypoint": 1, "car": 2, "wreck": 3, "barrier": 4}
    current = matrix.get(x_bin, y_bin)
    if priority.get(state, 0) > priority.get(current.state, 0):
        matrix.set(x_bin, y_bin, state, distance)


def _build_vision_matrix(
    state: CarRuntimeState,
    car: CarConfig,
    track,
    route_plan: CarRoutePlan,
    traffic: list[tuple[float, float, float, float, bool, bool]],
) -> VisionMatrix:
    matrix = VisionMatrix.empty()
    if not route_plan.permanent_waypoints:
        return matrix

    max_range = car.effective_view_distance
    heading = state.heading_radians
    half_angle = math.radians(35.0)
    x_edges = (-half_angle, -half_angle / 3.0, half_angle / 3.0, half_angle)
    y_edges = (0.0, max_range / 3.0, (max_range * 2.0) / 3.0, max_range)

    def _x_bin(angle_delta: float) -> str | None:
        if angle_delta < x_edges[0] or angle_delta > x_edges[3]:
            return None
        if angle_delta < x_edges[1]:
            return "left"
        if angle_delta <= x_edges[2]:
            return "center"
        return "right"

    def _y_bin(dist: float) -> str | None:
        if dist <= 0.0 or dist > y_edges[3]:
            return None
        if dist <= y_edges[1]:
            return "near"
        if dist <= y_edges[2]:
            return "middle"
        return "far"

    def _surface(px: float, py: float) -> bool:
        return point_in_polygon((px, py), track.outer_points, track._outer_bbox) and not point_in_polygon((px, py), track.inner_points, track._inner_bbox)

    # Barrier probes at each matrix cell center.
    x_offsets = {"left": -half_angle * 0.66, "center": 0.0, "right": half_angle * 0.66}
    y_centers = {"near": max_range / 6.0, "middle": max_range / 2.0, "far": max_range * 5.0 / 6.0}
    for y_bin in VISION_Y_BINS:
        for x_bin in VISION_X_BINS:
            ray_angle = heading + x_offsets[x_bin]
            dist = y_centers[y_bin]
            px = state.x + math.cos(ray_angle) * dist
            py = state.y + math.sin(ray_angle) * dist
            if not _surface(px, py):
                _vision_assign(matrix, x_bin, y_bin, "barrier", dist)

    # Obstacle/object occupancy.
    for ox, oy, _radius, _speed, is_wrecked, _is_stopped in traffic:
        dx = ox - state.x
        dy = oy - state.y
        dist = math.hypot(dx, dy)
        if dist > max_range or dist <= 1e-6:
            continue
        angle = _wrap_angle(math.atan2(dy, dx) - heading)
        xb = _x_bin(angle)
        yb = _y_bin(dist)
        if xb is None or yb is None:
            continue
        _vision_assign(matrix, xb, yb, "wreck" if is_wrecked else "car", dist)

    # Waypoint occupancy.
    target = route_plan.active_waypoint()
    if target is not None:
        dx = target.x - state.x
        dy = target.y - state.y
        dist = math.hypot(dx, dy)
        if dist <= max_range and dist > 1e-6:
            angle = _wrap_angle(math.atan2(dy, dx) - heading)
            xb = _x_bin(angle)
            yb = _y_bin(dist)
            if xb is not None and yb is not None:
                _vision_assign(matrix, xb, yb, "waypoint", dist)

    return matrix


def _closest_centerline_index(state: CarRuntimeState, centerline: list[tuple[float, float]]) -> int:
    best_index = 0
    best_dist = float("inf")
    for index, point in enumerate(centerline):
        dx = point[0] - state.x
        dy = point[1] - state.y
        dist = dx * dx + dy * dy
        if dist < best_dist:
            best_dist = dist
            best_index = index
    return best_index


def _turn_severity(centerline: list[tuple[float, float]], index: int) -> float:
    if len(centerline) < 3:
        return 0.0
    prev_pt = centerline[(index - 1) % len(centerline)]
    curr_pt = centerline[index]
    next_pt = centerline[(index + 1) % len(centerline)]

    in_angle = math.atan2(curr_pt[1] - prev_pt[1], curr_pt[0] - prev_pt[0])
    out_angle = math.atan2(next_pt[1] - curr_pt[1], next_pt[0] - curr_pt[0])
    delta = abs(_wrap_angle(out_angle - in_angle))
    return min(1.0, delta / math.pi)


def _blend_headings(a: float, b: float, weight_b: float) -> float:
    weight_b = max(0.0, min(1.0, weight_b))
    weight_a = 1.0 - weight_b
    x = weight_a * math.cos(a) + weight_b * math.cos(b)
    y = weight_a * math.sin(a) + weight_b * math.sin(b)
    return math.atan2(y, x)


def autonomous_controls(
    state: CarRuntimeState,
    car: CarConfig,
    track,
    race_elapsed: float,
    behavior: CarBehaviorProfile,
    learning: CarLearningState,
    traffic: list[tuple[float, float, float, float, bool, bool]],
    pass_side_bias: float,
    pace_bias: float,
    steer_bias: float,
    route_plan: CarRoutePlan | None = None,
    vision_matrix: VisionMatrix | None = None,
    last_visible_line_point: tuple[float, float] | None = None,
    preferred_line_offset: float = 0.0,
    stall_recover: bool = False,
    hard_recenter: bool = False,
    post_waypoint_boost: float = 0.0,
) -> tuple[float, float, float, str, str, tuple[float, float] | None]:
    centerline = _build_centerline(track)
    if len(centerline) < 2:
        return 0.0, 0.0, 0.0, "", "no_centerline", last_visible_line_point

    lane_width_samples = [
        math.hypot(track.outer_points[i][0] - track.inner_points[i][0], track.outer_points[i][1] - track.inner_points[i][1])
        for i in range(min(len(track.outer_points), len(track.inner_points)))
    ]
    avg_lane_width = sum(lane_width_samples) / max(1, len(lane_width_samples))

    nearest = _closest_centerline_index(state, centerline)
    if state.nav_last_index == nearest:
        state.nav_stall_frames += 1
    else:
        state.nav_last_index = nearest
        state.nav_stall_frames = 0

    heading_vec = (math.cos(state.heading_radians), math.sin(state.heading_radians))
    curr = centerline[nearest]
    plus = centerline[(nearest + 1) % len(centerline)]
    minus = centerline[(nearest - 1) % len(centerline)]
    plus_vec = (plus[0] - curr[0], plus[1] - curr[1])
    minus_vec = (minus[0] - curr[0], minus[1] - curr[1])
    dot_plus = heading_vec[0] * plus_vec[0] + heading_vec[1] * plus_vec[1]
    dot_minus = heading_vec[0] * minus_vec[0] + heading_vec[1] * minus_vec[1]
    if state.nav_direction not in (-1, 1):
        state.nav_direction = 1 if dot_plus >= dot_minus else -1
    forward_step = state.nav_direction

    speed_ratio = min(1.0, abs(state.speed) / max(car.max_speed, 1.0))
    severity_now = _turn_severity(centerline, nearest)
    lookahead = 1 + int(speed_ratio * 4.0 + severity_now * 2.0)
    lookahead = min(max(lookahead, 1), max(1, len(centerline) // 3))
    target = centerline[(nearest + forward_step * lookahead) % len(centerline)]
    target_next = centerline[(nearest + forward_step * (lookahead + 1)) % len(centerline)]

    route_points = centerline
    route_forward_step = forward_step
    if route_plan is not None and route_plan.permanent_waypoints:
        route_points = [(wp.x, wp.y) for wp in route_plan.permanent_waypoints]
        # Permanent route progression is always +1 index; using heading-derived
        # direction here can invert line-follow behavior and send cars backward.
        route_forward_step = 1

    def _point_to_segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        abx = bx - ax
        aby = by - ay
        apx = px - ax
        apy = py - ay
        ab_len2 = abx * abx + aby * aby
        if ab_len2 <= 1e-9:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab_len2))
        qx = ax + abx * t
        qy = ay + aby * t
        return math.hypot(px - qx, py - qy)

    def _route_line_distance(px: float, py: float) -> float:
        if len(route_points) < 2:
            return 0.0
        best = float("inf")
        for i in range(len(route_points)):
            ax, ay = route_points[i]
            bx, by = route_points[(i + 1) % len(route_points)]
            d = _point_to_segment_distance(px, py, ax, ay, bx, by)
            if d < best:
                best = d
        return best

    if route_plan is not None:
        active_wp = route_plan.active_waypoint()
        next_wp = route_plan.next_permanent_waypoint()
        if active_wp is not None:
            target = (active_wp.x, active_wp.y)
            if next_wp is not None:
                target_next = (next_wp.x, next_wp.y)
            route_plan.last_known_bearing = math.atan2(target[1] - state.y, target[0] - state.x)

    max_view_range = car.effective_view_distance
    view_half_angle = math.radians(35.0)

    waypoint_visible = True
    if route_plan is not None:
        active_wp = route_plan.active_waypoint()
        if active_wp is not None:
            dx = active_wp.x - state.x
            dy = active_wp.y - state.y
            dist = math.hypot(dx, dy)
            if dist > 1e-6:
                rel_angle = _wrap_angle(math.atan2(dy, dx) - state.heading_radians)
                ahead_proj = dx * math.cos(state.heading_radians) + dy * math.sin(state.heading_radians)
                waypoint_visible = (
                    dist <= max_view_range * 0.98
                    and abs(rel_angle) <= view_half_angle * 0.98
                    and ahead_proj > -6.0
                )

    nearest_route_idx = _nearest_centerline_index(route_points, state.x, state.y)

    # Line-follow-first targeting: use local route tangent/normal and a per-car
    # lateral offset so control does not hard-chase discrete waypoint points.
    if len(route_points) >= 3:
        line_anchor_idx = (nearest_route_idx + route_forward_step * 2) % len(route_points)
        line_next_idx = (nearest_route_idx + route_forward_step * 5) % len(route_points)
        line_prev = route_points[(line_anchor_idx - route_forward_step) % len(route_points)]
        line_anchor = route_points[line_anchor_idx]
        line_next = route_points[line_next_idx]

        tan_x = line_next[0] - line_prev[0]
        tan_y = line_next[1] - line_prev[1]
        tan_len = math.hypot(tan_x, tan_y)
        if tan_len > 1e-6:
            tan_x /= tan_len
            tan_y /= tan_len
            normal_x = -tan_y
            normal_y = tan_x

            offset_anchor = (
                line_anchor[0] + normal_x * preferred_line_offset,
                line_anchor[1] + normal_y * preferred_line_offset,
            )
            offset_next = (
                line_next[0] + normal_x * preferred_line_offset,
                line_next[1] + normal_y * preferred_line_offset,
            )

            target = (
                target[0] * 0.3 + offset_anchor[0] * 0.7,
                target[1] * 0.3 + offset_anchor[1] * 0.7,
            )
            target_next = (
                target_next[0] * 0.25 + offset_next[0] * 0.75,
                target_next[1] * 0.25 + offset_next[1] * 0.75,
            )

    line_visible = False
    visible_line_point: tuple[float, float] | None = None
    for hop in (1, 2, 3, 4, 5):
        rp = route_points[(nearest_route_idx + route_forward_step * hop) % len(route_points)]
        dx = rp[0] - state.x
        dy = rp[1] - state.y
        dist = math.hypot(dx, dy)
        if dist <= 1e-6 or dist > max_view_range * 0.98:
            continue
        rel_angle = _wrap_angle(math.atan2(dy, dx) - state.heading_radians)
        if abs(rel_angle) <= view_half_angle * 0.98:
            line_visible = True
            visible_line_point = rp
            break

    if line_visible and visible_line_point is not None:
        # Hysteresis: smooth visible line-point updates to avoid frame-to-frame
        # heading flip-flop between nearby route samples.
        if last_visible_line_point is None:
            last_visible_line_point = visible_line_point
        else:
            blend = 0.28
            last_visible_line_point = (
                last_visible_line_point[0] * (1.0 - blend) + visible_line_point[0] * blend,
                last_visible_line_point[1] * (1.0 - blend) + visible_line_point[1] * blend,
            )

    if not waypoint_visible and len(route_points) > 1:
        # If the next waypoint is not visible, continue driving along the route line.
        target = route_points[(nearest_route_idx + route_forward_step * 2) % len(route_points)]
        target_next = route_points[(nearest_route_idx + route_forward_step * 4) % len(route_points)]

    if hard_recenter:
        # Temporarily prioritize getting back to centerline flow after watchdog recovery.
        target = centerline[(nearest + forward_step) % len(centerline)]
        target_next = centerline[(nearest + forward_step * 2) % len(centerline)]

    if state.nav_stall_frames > 120:
        path_heading_recover = math.atan2(target_next[1] - target[1], target_next[0] - target[0])
        state.heading_radians = _blend_headings(state.heading_radians, path_heading_recover, 0.55)
        state.speed = max(state.speed, 10.0)
        state.nav_last_index = nearest
        state.nav_stall_frames = 0

    nearest_point = centerline[nearest]
    dist_to_center = math.hypot(nearest_point[0] - state.x, nearest_point[1] - state.y)
    local_track_width = max(1.0, avg_lane_width)
    off_center = dist_to_center > local_track_width * 0.35
    route_line_dist = _route_line_distance(state.x, state.y)
    off_route_line = route_line_dist > max(18.0, local_track_width * 0.34)
    off_track = not point_in_polygon((state.x, state.y), track.outer_points, track._outer_bbox) or point_in_polygon((state.x, state.y), track.inner_points, track._inner_bbox)
    wall_contact_recover = state.wall_contact_frames > 0
    off_track_like = off_track or wall_contact_recover
    launch_phase = state.laps == 0 and (
        state.distance_traveled < max(420.0, local_track_width * 6.0)
        or race_elapsed < 4.0
    )

    hard_hazard_ahead = False
    if vision_matrix is not None:
        near_center_state = vision_matrix.get("center", "near").state
        mid_center_state = vision_matrix.get("center", "mid").state
        speed_now = max(0.0, state.speed)
        if near_center_state in ("barrier", "wreck"):
            hard_hazard_ahead = True
        elif near_center_state == "car" and speed_now > 20.0 and not launch_phase:
            hard_hazard_ahead = True
        elif mid_center_state in ("barrier", "wreck") and speed_now > 24.0:
            hard_hazard_ahead = True

    # R4 / temporary avoidance waypoint: if a stopped/wrecked hazard blocks the
    # line to the active permanent waypoint, steer toward a transient surface
    # point just past the hazard on a clear side so the car commits to driving
    # around it instead of panicking. Recomputed each frame (runtime-only).
    def _r4_on_surface(px: float, py: float) -> bool:
        return point_in_polygon((px, py), track.outer_points, track._outer_bbox) and not point_in_polygon(
            (px, py), track.inner_points, track._inner_bbox
        )

    if route_plan is not None and route_plan.active_waypoint() is not None and traffic and not hard_recenter:
        aw_x, aw_y = target
        aw_fwd_x = math.cos(state.heading_radians)
        aw_fwd_y = math.sin(state.heading_radians)
        blocked_wreck = None
        for (box, boy, bradius, _bs, bis_wrecked, bis_stopped) in traffic:
            if not (bis_wrecked or bis_stopped):
                continue
            if (box - state.x) * aw_fwd_x + (boy - state.y) * aw_fwd_y <= 0.0:
                continue
            if _point_to_segment_distance(box, boy, state.x, state.y, aw_x, aw_y) < bradius + car.width * 0.5 + 6.0:
                blocked_wreck = (box, boy, bradius)
                break
        if blocked_wreck is not None and next_wp is not None:
            box, boy, bradius = blocked_wreck
            wside = (box - state.x) * (-aw_fwd_y) + (boy - state.y) * aw_fwd_x
            pass_sign = -1.0 if wside >= 0.0 else 1.0
            wlat = max(30.0, bradius * 1.5 + car.width * 0.6)
            wahead = max(55.0, bradius + car.length)
            ax = box + aw_fwd_x * wahead + (-aw_fwd_y) * (pass_sign * wlat)
            ay = boy + aw_fwd_y * wahead + aw_fwd_x * (pass_sign * wlat)
            if not _r4_on_surface(ax, ay):
                ax = box + aw_fwd_x * (wahead * 0.5) + (-aw_fwd_y) * (pass_sign * wlat * 0.5)
                ay = boy + aw_fwd_y * (wahead * 0.5) + aw_fwd_x * (pass_sign * wlat * 0.5)
            if _r4_on_surface(ax, ay):
                target = (ax, ay)
                target_next = (next_wp.x, next_wp.y)

    target_heading = math.atan2(target[1] - state.y, target[0] - state.x)
    path_heading = math.atan2(target_next[1] - target[1], target_next[0] - target[0])
    path_forward_x = math.cos(path_heading)
    path_forward_y = math.sin(path_heading)
    dist_to_target = math.hypot(target[0] - state.x, target[1] - state.y)
    turn_in_distance = max(local_track_width * 2.1, 120.0)
    path_weight = 0.35 if dist_to_target > turn_in_distance else 0.78
    desired_heading = _blend_headings(target_heading, path_heading, path_weight)
    if off_center:
        recover_point = centerline[(nearest + forward_step) % len(centerline)]
        recover_heading = math.atan2(recover_point[1] - state.y, recover_point[0] - state.x)
        desired_heading = _blend_headings(desired_heading, recover_heading, 0.35)
    if off_track_like:
        reentry_heading = math.atan2(nearest_point[1] - state.y, nearest_point[0] - state.x)
        desired_heading = _blend_headings(desired_heading, reentry_heading, 0.72)

    forward_x = math.cos(state.heading_radians)
    forward_y = math.sin(state.heading_radians)

    to_target_x = target[0] - state.x
    to_target_y = target[1] - state.y
    to_target_dist = max(1e-6, math.hypot(to_target_x, to_target_y))
    to_target_dir_x = to_target_x / to_target_dist
    to_target_dir_y = to_target_y / to_target_dist
    target_ahead_projection = to_target_x * forward_x + to_target_y * forward_y
    velocity_toward_target = state.vx * to_target_dir_x + state.vy * to_target_dir_y
    start_rect = pygame.Rect(track.start_grid)
    start_cx = start_rect.x + start_rect.w * 0.5
    start_cy = start_rect.y + start_rect.h * 0.5
    start_dist = math.hypot(state.x - start_cx, state.y - start_cy)
    start_line_commit = (
        (state.left_start_zone or state.laps == 0)
        and start_dist < max(start_rect.w, start_rect.h) * 2.2
        and to_target_dist < max(240.0, local_track_width * 3.0)
        and target_ahead_projection > -4.0
        and not off_track_like
    )
    wrong_way_recover = (
        to_target_dist > max(30.0, local_track_width * 0.65)
        and target_ahead_projection < -2.0
        and velocity_toward_target < -1.5
    )
    progress_commit = (
        not wrong_way_recover
        and not off_track_like
        and to_target_dist > max(42.0, local_track_width * 0.8)
        and target_ahead_projection > 2.0
        and velocity_toward_target < 4.0
    )
    if wrong_way_recover:
        # Hard-bias orientation back toward target and slow down until car stops moving away.
        desired_heading = _blend_headings(desired_heading, target_heading, 0.82)
    if stall_recover:
        desired_heading = _blend_headings(desired_heading, target_heading, 0.78)

    if (
        line_visible
        and last_visible_line_point is not None
        and not hard_hazard_ahead
        and not wrong_way_recover
        and not hard_recenter
    ):
        # When the line is visible and hazards are not urgent, keep line lock
        # dominant to prevent heading oscillation away from the visible line.
        line_lock_heading = math.atan2(
            last_visible_line_point[1] - state.y,
            last_visible_line_point[0] - state.x,
        )
        desired_heading = _blend_headings(desired_heading, line_lock_heading, 0.82)

    if not line_visible:
        # If the line is lost, rotate toward the last known visible line point
        # before falling back to nearest forward route direction.
        recover_pt = last_visible_line_point
        if recover_pt is not None:
            rdx = recover_pt[0] - state.x
            rdy = recover_pt[1] - state.y
            rdist = math.hypot(rdx, rdy)
            rahead = rdx * forward_x + rdy * forward_y
            # Ignore stale memory points that are behind or too far away.
            if rdist > max_view_range * 1.3 or rahead < -6.0:
                recover_pt = None
                last_visible_line_point = None
        if recover_pt is None:
            recover_pt = route_points[(nearest_route_idx + route_forward_step * 2) % len(route_points)]
        recover_heading = math.atan2(recover_pt[1] - state.y, recover_pt[0] - state.x)
        desired_heading = _blend_headings(desired_heading, recover_heading, 0.9)

    severity_ahead = _turn_severity(centerline, (nearest + forward_step * lookahead) % len(centerline))
    # Deterministic corner forecast across a longer lookahead window.
    forecast_steps = (2, 4, 6, 8, 10)
    forecast_severities = [
        _turn_severity(centerline, (nearest + forward_step * step) % len(centerline))
        for step in forecast_steps
    ]
    severity_forecast_max = max(forecast_severities) if forecast_severities else severity_ahead
    severity_forecast_avg = sum(forecast_severities) / max(1, len(forecast_severities))

    # Traffic-aware adjustment: avoid cars ahead while staying stable through corners.
    def _is_surface(px: float, py: float) -> bool:
        return point_in_polygon((px, py), track.outer_points, track._outer_bbox) and not point_in_polygon((px, py), track.inner_points, track._inner_bbox)

    def _margin_probe(forward_offset: float, side_offset: float) -> bool:
        px = state.x + forward_x * forward_offset + (-forward_y) * side_offset
        py = state.y + forward_y * forward_offset + forward_x * side_offset
        return _is_surface(px, py)

    # Probe just outside each side of the footprint so we can react before actual wall contact.
    margin = max(6.0, car.width * 0.2)
    side_extent = car.width * 0.5 + margin
    forward_offsets = (car.length * 0.42, 0.0, -car.length * 0.42)
    left_margin_hits = 0
    right_margin_hits = 0
    for fwd in forward_offsets:
        if not _margin_probe(fwd, side_extent):
            left_margin_hits += 1
        if not _margin_probe(fwd, -side_extent):
            right_margin_hits += 1

    near_barrier_margin = (left_margin_hits + right_margin_hits) > 0
    barrier_avoid_steer = max(-0.42, min(0.42, (left_margin_hits - right_margin_hits) * 0.2))
    barrier_center_recover = near_barrier_margin and dist_to_center > local_track_width * 0.22

    if near_barrier_margin and not off_track:
        recover_point = centerline[(nearest + forward_step) % len(centerline)]
        recover_heading = math.atan2(recover_point[1] - state.y, recover_point[0] - state.x)
        desired_heading = _blend_headings(desired_heading, recover_heading, 0.7 if barrier_center_recover else 0.62)

    def _choose_pass_side(other_ahead: float, other_side: float, other_radius: float) -> float:
        if abs(other_side) >= 3.0:
            return -1.0 if other_side >= 0.0 else 1.0

        base_clear = max(14.0, car.width * 0.8 + other_radius * 0.35)
        ahead_samples = (
            min(55.0, max(22.0, other_ahead * 0.35)),
            min(95.0, max(36.0, other_ahead * 0.65)),
            min(135.0, max(52.0, other_ahead * 0.95)),
        )

        def _score(side_dir: float) -> int:
            score = 0
            for ahead_dist in ahead_samples:
                for width_mult in (1.0, 1.55):
                    lateral = base_clear * width_mult * side_dir
                    probe_x = state.x + forward_x * ahead_dist + (-forward_y) * lateral
                    probe_y = state.y + forward_y * ahead_dist + forward_x * lateral
                    if _is_surface(probe_x, probe_y):
                        score += 1
            return score

        right_score = _score(1.0)
        left_score = _score(-1.0)
        if right_score == left_score:
            return 1.0 if pass_side_bias >= 0.0 else -1.0
        return 1.0 if right_score > left_score else -1.0

    avoid_steer = 0.0
    slowdown_factor = 1.0
    emergency_brake = False
    nearest_ahead = float("inf")
    nearest_stopped_ahead = float("inf")
    nearest_moving_ahead = float("inf")
    moving_ahead_closing = 0.0
    traffic_pressure = 0.0
    # R1/R2/R4: track pass-commit vs blocked state around stopped hazards.
    stopped_pass_clear = False   # committed to a clear pass lane around a wreck
    stopped_blocked = False      # dead-center obstruction, no clear pass on either side
    for ox, oy, other_radius, other_speed, is_wrecked, is_stopped in traffic:
        rel_x = ox - state.x
        rel_y = oy - state.y
        ahead = rel_x * forward_x + rel_y * forward_y
        ahead_on_path = rel_x * path_forward_x + rel_y * path_forward_y
        if ahead <= 0.0:
            continue
        if ahead_on_path <= 0.0:
            continue
        stopped_hazard = is_wrecked or is_stopped
        if launch_phase and is_stopped and not is_wrecked and ahead < 95.0:
            stopped_hazard = False
        max_ahead = 300.0 if stopped_hazard else 180.0
        if ahead > max_ahead:
            continue

        side = rel_x * (-forward_y) + rel_y * forward_x
        lateral_limit = max(24.0, (car.width + other_radius * (2.4 if stopped_hazard else 1.4)) * 1.0)
        if abs(side) > lateral_limit:
            # Already beside this obstacle; if it's a stopped hazard,
            # that implies a pass lane is open (or at least usable).
            if stopped_hazard:
                stopped_pass_clear = True
            continue

        nearest_ahead = min(nearest_ahead, ahead)
        if stopped_hazard:
            nearest_stopped_ahead = min(nearest_stopped_ahead, ahead)
        side_sign = _choose_pass_side(ahead, side, other_radius)
        proximity = max(0.0, 1.0 - ahead / 180.0)
        traffic_pressure = max(traffic_pressure, proximity)

        steer_gain = 0.25 + proximity * 0.45
        if stopped_hazard:
            steer_gain += 0.34
        if severity_now > 0.16 or severity_ahead > 0.18:
            if stopped_hazard:
                # Keep full evasive authority when a stopped hazard is close in a corner.
                steer_gain *= 1.0 if ahead < 170.0 else 0.7
            else:
                steer_gain *= 0.3
        avoid_steer += side_sign * steer_gain

        effective_other_speed = 0.0 if stopped_hazard else other_speed
        closing_speed = max(0.0, state.speed - effective_other_speed)
        if not stopped_hazard and ahead < nearest_moving_ahead:
            nearest_moving_ahead = ahead
            moving_ahead_closing = closing_speed
        if closing_speed > (2.0 if stopped_hazard else 4.0):
            base_slow = 1.0 - proximity * (1.1 if stopped_hazard else 0.9)
            slowdown_factor = min(slowdown_factor, max(0.16 if stopped_hazard else 0.22, base_slow))
        if stopped_hazard:
            # R1: gate emergency brake on whether a clear pass lane is present.
            # Once the car is committed to a pass side (the _choose_pass_side
            # probes show a clear path), disable the full-stop order so the
            # car drives past instead of re-slamming the brakes every frame.
            pass_side = side_sign
            pass_clear = False
            # Check surface probes on the chosen pass side, similar to what
            # _choose_pass_side's _score does but for the nearest hazard.
            base_clear = max(14.0, car.width * 0.8 + other_radius * 0.35)
            probe_dist = min(55.0, max(22.0, ahead * 0.5))
            for width_mult in (1.0, 1.55):
                lat = base_clear * width_mult * pass_side
                px = state.x + forward_x * probe_dist + (-forward_y) * lat
                py = state.y + forward_y * probe_dist + forward_x * lat
                if _is_surface(px, py):
                    pass_clear = True
                    break
            if pass_clear:
                stopped_pass_clear = True
            else:
                stopped_blocked = True
            emergency_distance = max(105.0, 74.0 + max(0.0, state.speed) * 0.95 + closing_speed * 2.5)
            if ahead < emergency_distance and closing_speed > 0.5 and not start_line_commit and not pass_clear:
                emergency_brake = True
        elif ahead < 54.0 and closing_speed > 2.0:
            emergency_brake = True

    pass_intent = (
        nearest_moving_ahead < 155.0
        and moving_ahead_closing > 4.5
        and severity_now < 0.11
        and severity_ahead < 0.13
        and nearest_stopped_ahead > 170.0
        and not off_track
        and not off_center
    )
    if pass_intent:
        # Bias to commit to a pass line instead of queuing behind a slower car.
        pass_side = 1.0 if pass_side_bias >= 0.0 else -1.0
        if abs(avoid_steer) < 0.22:
            avoid_steer += pass_side * 0.22
        slowdown_factor = max(slowdown_factor, 0.84)

    if line_visible and not hard_hazard_ahead and not pass_intent and not near_barrier_margin:
        # Dampen lateral avoidance oscillation while line lock is strong and safe.
        avoid_steer *= 0.45

    avoid_limit = 0.75 if nearest_stopped_ahead < 160.0 else 0.55
    if near_barrier_margin:
        avoid_steer += barrier_avoid_steer
        slowdown_factor = min(slowdown_factor, 0.5 if barrier_center_recover else 0.56)
        avoid_limit = max(avoid_limit, 0.78)
    avoid_steer = max(-avoid_limit, min(avoid_limit, avoid_steer))

    if vision_matrix is not None:
        near_center = vision_matrix.get("center", "near").state
        speed_now = max(0.0, state.speed)
        if near_center in ("wreck", "car", "barrier"):
            if near_center == "car" and (launch_phase or start_line_commit):
                # Avoid pack deadlock at launch: steer and soften speed instead of full brake.
                emergency_brake = False
                slowdown_factor = min(slowdown_factor, 0.72)
            elif near_center == "barrier" and start_line_commit and speed_now < 28.0:
                emergency_brake = False
                slowdown_factor = min(slowdown_factor, 0.76)
            elif stopped_pass_clear:
                # R1: the car is already committed to a clear pass lane around a
                # wreck that still shows up in the center band — soften speed and
                # steer instead of forcing another full emergency stop.
                emergency_brake = False
                slowdown_factor = min(slowdown_factor, 0.8)
            else:
                emergency_brake = True
            left_score = sum(1 for y_bin in VISION_Y_BINS if vision_matrix.get("left", y_bin).state in ("clear", "waypoint"))
            right_score = sum(1 for y_bin in VISION_Y_BINS if vision_matrix.get("right", y_bin).state in ("clear", "waypoint"))
            if right_score > left_score:
                avoid_steer += 0.32
            elif left_score > right_score:
                avoid_steer -= 0.32
            avoid_steer = max(-avoid_limit, min(avoid_limit, avoid_steer))

    heading_error = _wrap_angle(desired_heading - state.heading_radians)
    segment_heading = math.atan2(target[1] - nearest_point[1], target[0] - nearest_point[0])
    turn_feedforward = _wrap_angle(path_heading - segment_heading)
    steering_gain = (3.2 + severity_now * 2.0) * learning.steering_aggression * steer_bias
    if off_center:
        steering_gain += 1.4
    if hard_recenter:
        steering_gain += 1.15
    steering_cmd = heading_error * steering_gain + turn_feedforward * 0.9
    steering_cmd += avoid_steer
    steering = max(-1.0, min(1.0, steering_cmd))

    severity = max(severity_now, severity_ahead)
    in_turn = severity > 0.12
    base_target = car.max_speed * (0.11 + (1.0 - severity) * 0.13)
    angle_factor = max(0.12, 1.0 - (abs(heading_error) / math.pi) * 1.6)

    speed_priority_scale = 1.0 + (behavior.speed_priority + behavior.avoid_slowdown_priority) * 0.09
    speed_priority_scale *= pace_bias
    forward_speed = max(0.0, state.speed)
    target_speed = min(car.max_speed, base_target * angle_factor * speed_priority_scale * learning.target_speed_bias)

    # Stronger turn-entry protection with geometric forecast.
    turn_entry_severity = max(
        0.0,
        severity_forecast_max * 1.4 + severity_ahead * 0.9 + severity_now * 0.45,
    )
    if severity_forecast_max > 0.1 or severity_ahead > 0.12 or launch_phase:
        entry_cap = car.max_speed * (0.045 + (1.0 - min(1.0, turn_entry_severity)) * 0.072)
        if launch_phase:
            entry_cap *= 0.82
        # Keep minimum entry speeds low enough to avoid broad overshoot.
        target_speed = min(target_speed, max(11.0, entry_cap))

    if launch_phase:
        target_speed = min(target_speed, max(18.0, car.max_speed * 0.085))
    if wrong_way_recover:
        target_speed = min(target_speed, 12.0)

    # Keep straight-line pace track-aware; very high configured max_speed values
    # can otherwise produce runaway speeds that overshoot waypoints on small tracks.
    straight_speed_cap = max(34.0, min(72.0, local_track_width * 1.25))
    if not in_turn:
        target_speed = min(target_speed, straight_speed_cap)
        if abs(heading_error) > 0.2 or near_barrier_margin:
            target_speed = min(target_speed, straight_speed_cap * 0.8)
    if stall_recover:
        target_speed = min(target_speed, max(14.0, straight_speed_cap * 0.55))
    if hard_recenter:
        recenter_cap = max(12.0, min(24.0, local_track_width * 0.42))
        if near_barrier_margin or off_track_like or dist_to_center > local_track_width * 0.28:
            recenter_cap = min(recenter_cap, 16.0)
        target_speed = min(target_speed, recenter_cap)

    turn_entry_risk = (
        severity_forecast_max > 0.12
        or severity_ahead > 0.1
        or abs(heading_error) > 0.36
        or off_route_line
        or not line_visible
    )

    if (
        post_waypoint_boost > 0.0
        and not off_route_line
        and line_visible
        and abs(heading_error) < 0.45
        and not turn_entry_risk
        and forward_speed < straight_speed_cap * 1.12
    ):
        boost_floor = max(target_speed, min(car.max_speed, max(42.0, local_track_width * 1.12)))
        target_speed = boost_floor

    if not line_visible:
        target_speed = min(target_speed, max(12.0, local_track_width * 0.42))

    target_speed *= slowdown_factor
    if nearest_ahead < float("inf"):
        follow_cap = max(12.0, nearest_ahead * 0.45)
        if severity > 0.16:
            follow_cap *= 0.8
        if pass_intent:
            follow_cap = max(follow_cap, min(car.max_speed, forward_speed + 9.0 + moving_ahead_closing * 0.6))
        target_speed = min(target_speed, follow_cap)

    damage_factor = max(0.35, 1.0 - (state.damage / 160.0) * learning.safety_bias)
    target_speed *= damage_factor
    if off_center:
        target_speed = min(target_speed, 30.0)
    if off_track:
        target_speed = min(target_speed, 18.0)

    # Keep momentum through corners to avoid stop-and-go behavior.
    corner_carry_speed = max(18.0, min(34.0, car.max_speed * 0.075))
    if in_turn:
        target_speed = max(target_speed, corner_carry_speed)

    speed_error = target_speed - forward_speed
    throttle = 0.0
    brake = 0.0
    brake_reason = ""
    coast_reason = ""
    coast_phase = False
    exit_phase = (
        not off_track
        and severity_now > 0.1
        and severity_ahead < severity_now * 0.78
        and abs(heading_error) < 0.38
        and not off_center
        and nearest_ahead > 86.0
        and nearest_stopped_ahead > 150.0
    )

    # True coast phase: release both pedals when speed is close to target,
    # especially in corners, to carry inertia smoothly.
    if not emergency_brake and not off_track_like and not exit_phase and not stall_recover and not hard_recenter:
        if in_turn and abs(speed_error) <= 4.5 and abs(heading_error) <= 0.5 and nearest_ahead > 72.0:
            coast_phase = True
            coast_reason = "turn_speed_match"
        elif not in_turn and abs(speed_error) <= 3.0 and nearest_ahead > 80.0 and (not launch_phase or forward_speed < 42.0):
            coast_phase = True
            coast_reason = "straight_speed_match"

    if not coast_phase:
        if speed_error > 2.5:
            throttle = 1.0
    if pass_intent and not emergency_brake and speed_error > -4.0:
        throttle = max(throttle, 0.7)
    if exit_phase and speed_error > -2.0:
        throttle = max(throttle, 0.8)

    if launch_phase:
        throttle = min(throttle, 0.42)
    if hard_recenter:
        throttle = min(throttle, 0.45)
    if not line_visible and not emergency_brake:
        # Hunt for the route line by turning decisively with gentle throttle.
        throttle = max(throttle, 0.25)
    if wrong_way_recover:
        throttle = 0.0
    if start_line_commit and not emergency_brake and not wrong_way_recover:
        throttle = max(throttle, 0.34)
    if progress_commit and not emergency_brake and not start_line_commit:
        throttle = max(throttle, 0.5)

    # Hard guard for corner entry to prevent turn overshoot.
    if severity_forecast_max > 0.14 or (severity_ahead > 0.1 and abs(heading_error) > 0.26):
        throttle = min(throttle, 0.28)
    overspeed = forward_speed - target_speed

    straight_cruise = (
        severity < 0.1
        and severity_ahead < 0.12
        and abs(heading_error) < 0.28
        and nearest_ahead > 115.0
        and not off_center
    )

    safety_turn_guard = 1.0 + behavior.barrier_avoidance_priority * 0.25 + (learning.safety_bias - 1.0) * 0.3

    def _set_brake(value: float, reason: str) -> None:
        nonlocal brake, brake_reason
        if value > brake:
            brake = value
            brake_reason = reason

    if not coast_phase:
        if overspeed > 12.0:
            _set_brake(min(0.85, overspeed / 22.0), "overspeed")
        if near_barrier_margin and forward_speed > 19.0:
            _set_brake(0.48, "barrier_margin_guard")
        if launch_phase and forward_speed > 38.0:
            _set_brake(0.7, "launch_speed_cap")
        if severity_forecast_max > 0.1 and forward_speed > target_speed + 1.0:
            setup_brake = min(0.9, 0.48 + severity_forecast_max * 1.25)
            _set_brake(setup_brake, "turn_entry_setup")
        if severity_forecast_max > 0.16 and forward_speed > 20.0:
            _set_brake(0.74, "turn_entry_hard_guard")
        if severity_forecast_avg > 0.12 and forward_speed > 18.0:
            _set_brake(0.66, "turn_forecast_guard")
        if severity_ahead > 0.09 and abs(heading_error) > 0.4 and forward_speed > 18.0:
            _set_brake(0.68, "turn_heading_prevent_overshoot")
        if in_turn and severity_ahead * safety_turn_guard > 0.24 and forward_speed > 26.0:
            _set_brake(0.55, "turn_guard")
        if in_turn and forward_speed > 24.0 and abs(heading_error) > 0.55:
            _set_brake(0.5, "turn_heading_error")
        if in_turn and off_center and forward_speed > 28.0:
            _set_brake(0.65, "turn_off_center")
        if state.damage > 55.0 and forward_speed > target_speed:
            _set_brake(0.55, "high_damage")
        if nearest_ahead < 64.0 and forward_speed > target_speed + 4.0:
            _set_brake(0.6, "traffic_close")
        if in_turn and traffic_pressure > 0.35 and forward_speed > 22.0:
            _set_brake(0.5, "turn_traffic_pressure")
        if nearest_stopped_ahead < 190.0 and forward_speed > 24.0 and not stopped_pass_clear:
            approach_pressure = max(0.0, 1.0 - nearest_stopped_ahead / 190.0)
            _set_brake(0.35 + approach_pressure * 0.4, "stopped_hazard_approach")
        if nearest_stopped_ahead < 115.0 and forward_speed > 18.0 and not stopped_pass_clear:
            _set_brake(0.58, "stopped_hazard_near")
        if wrong_way_recover:
            _set_brake(0.9, "wrong_way_recover")
        if stall_recover and forward_speed > target_speed + 1.0:
            _set_brake(0.52, "route_stall_recover")
        if hard_recenter and forward_speed > target_speed + 1.0:
            _set_brake(0.58, "hard_route_recenter")

    if launch_phase and forward_speed > 44.0:
        throttle = 0.0
        _set_brake(1.0, "launch_hard_cap")
    if start_line_commit and not wrong_way_recover:
        brake = min(brake, 0.22)
        if brake <= 0.0:
            brake_reason = ""
    if progress_commit and not wrong_way_recover and not emergency_brake:
        brake = min(brake, 0.15)
        if brake <= 0.0:
            brake_reason = ""
    if wall_contact_recover:
        throttle = min(throttle, 0.22)
        _set_brake(max(brake, 0.72), "wall_contact_recover")
    if (
        straight_cruise
        and not emergency_brake
        and not off_track_like
        and not wall_contact_recover
        and not wrong_way_recover
        and not near_barrier_margin
        and not stall_recover
        and not hard_recenter
    ):
        brake = 0.0
        brake_reason = ""
        if speed_error <= 2.0:
            throttle = 0.0
    if pass_intent and not emergency_brake:
        brake = min(brake, 0.2)
        if brake <= 0.0:
            brake_reason = ""
    if exit_phase and not emergency_brake:
        brake = min(brake, 0.2)
        if brake <= 0.0:
            brake_reason = ""
    if off_track_like:
        throttle = 0.0
        if forward_speed > 14.0:
            _set_brake(0.6, "off_track_recovery")
        elif abs(heading_error) < 0.35 and forward_speed < 10.0:
            throttle = 0.4

    if (
        stall_recover
        and line_visible
        and not off_track_like
        and nearest_stopped_ahead > 140.0
        and nearest_ahead > 90.0
    ):
        # Drive out of stall aggressively when there is no close hazard ahead.
        throttle = max(throttle, 0.72)
        brake = min(brake, 0.18)
        if brake <= 0.0:
            brake_reason = ""

    if (
        post_waypoint_boost > 0.0
        and not off_route_line
        and line_visible
        and abs(heading_error) < 0.45
        and not turn_entry_risk
        and not emergency_brake
    ):
        throttle = max(throttle, 0.75)

    if not line_visible and not emergency_brake:
        steering = max(-1.0, min(1.0, steering * 1.25))
        brake = min(brake, 0.2)
        if brake <= 0.0:
            brake_reason = ""
        # Forward bias when the line is lost: if the car is genuinely on the
        # racing surface and roughly aimed forward, power out of the lost state
        # instead of only steering toward a possibly behind/off-track recovery
        # target while crawling.
        if (
            not off_track_like
            and not off_track
            and abs(heading_error) < 0.5
            and not hard_recenter
        ):
            throttle = max(throttle, 0.6)

    # R2: committed to a clear pass lane around a wreck — keep a moderate
    # throttle floor and avoid hard braking so the car actually drives past
    # the hazard instead of coasting back into the brake band.
    if stopped_pass_clear and not emergency_brake and not off_track_like and abs(heading_error) < 0.5:
        throttle = max(throttle, 0.4)
        brake = min(brake, 0.15)
        if brake <= 0.0:
            brake_reason = ""

    # R3: the hazard fully blocks the track ahead (no usable pass lane) and we
    # are slow and close — creep through at low speed rather than freezing
    # forever in front of the pileup.
    if (
        stopped_blocked
        and not stopped_pass_clear
        and abs(state.speed) < 14.0
        and nearest_stopped_ahead < 120.0
        and not off_track_like
    ):
        emergency_brake = False
        throttle = max(throttle, 0.2)
        brake = min(brake, 0.28)
        if brake <= 0.0:
            brake_reason = ""

    if emergency_brake:
        brake = 1.0
        brake_reason = "emergency_hazard"

    return throttle, brake, steering, brake_reason, coast_reason, last_visible_line_point


def _centerline_length(track) -> float:
    if track._centerline_length > 0.0:
        return track._centerline_length
    count = min(len(track.outer_points), len(track.inner_points))
    if count < 2:
        return 0.0
    points = [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(count)
    ]
    total = 0.0
    for i in range(len(points)):
        a = points[i]
        b = points[(i + 1) % len(points)]
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def update_lap_counter(state: CarRuntimeState, track) -> None:
    start_rect = pygame.Rect(track.start_grid)
    in_start = start_rect.collidepoint(state.x, state.y)
    passed_through_start = bool(start_rect.clipline(state.last_x, state.last_y, state.x, state.y))

    center_x = start_rect.x + start_rect.w * 0.5
    center_y = start_rect.y + start_rect.h * 0.5
    away_dist = math.hypot(state.x - center_x, state.y - center_y)
    if not in_start and away_dist > max(start_rect.w, start_rect.h) * 0.75:
        state.left_start_zone = True

    if (in_start or passed_through_start) and state.left_start_zone:
        lap_len = _centerline_length(track)
        min_lap_distance = max(80.0, lap_len * 0.2)
        if state.distance_traveled - state.last_lap_distance >= min_lap_distance:
            state.laps += 1
            state.left_start_zone = False
            state.cumulative_angle = 0.0
            state.last_lap_distance = state.distance_traveled


def _make_unique_car_name(base_name: str, cars: list[SimCar]) -> str:
    existing = {entry.instance_name for entry in cars}
    if base_name not in existing:
        return base_name
    suffix = 2
    while True:
        candidate = f"{base_name}_{suffix}"
        if candidate not in existing:
            return candidate
        suffix += 1


def _heading_from_track_position(track, x: float, y: float) -> float:
    centerline = _build_centerline(track)
    if len(centerline) < 2:
        return -math.pi / 2
    nearest_index = _nearest_centerline_index(centerline, x, y)
    return _ccw_spawn_heading(centerline, nearest_index)


def _is_on_surface(track, x: float, y: float) -> bool:
    return point_in_polygon((x, y), track.outer_points, track._outer_bbox) and not point_in_polygon((x, y), track.inner_points, track._inner_bbox)


def _car_collision_radius(car: CarConfig) -> float:
    return max(8.0, math.hypot(car.length, car.width) * 0.34)


def _car_overlaps_any(sim_cars: list[SimCar], idx: int, x: float, y: float, car: CarConfig) -> bool:
    radius = _car_collision_radius(car)
    for other_idx, other in enumerate(sim_cars):
        if other_idx == idx:
            continue
        other_radius = _car_collision_radius(other.config)
        min_dist = radius + other_radius + 4.0
        dx = other.state.x - x
        dy = other.state.y - y
        if dx * dx + dy * dy < min_dist * min_dist:
            return True
    return False


def _find_open_pose(
    track,
    sim_cars: list[SimCar],
    idx: int,
    car: CarConfig,
    base_pose: tuple[float, float, float],
) -> tuple[float, float, float]:
    bx, by, heading = base_pose

    # Deterministic stagger near the start grid to reduce launch bunching.
    fwd_x = math.cos(heading)
    fwd_y = math.sin(heading)
    side_x = -fwd_y
    side_y = fwd_x
    lane_pattern = (0, -1, 1)
    lane_slot = lane_pattern[idx % len(lane_pattern)]
    row_slot = idx // len(lane_pattern)
    forward_spacing = max(car.length * 1.7, 86.0)
    lateral_spacing = max(car.width * 1.35, 34.0)
    staged_x = bx - fwd_x * (row_slot * forward_spacing) + side_x * (lane_slot * lateral_spacing)
    staged_y = by - fwd_y * (row_slot * forward_spacing) + side_y * (lane_slot * lateral_spacing)
    if _is_on_surface(track, staged_x, staged_y) and not _car_overlaps_any(sim_cars, idx, staged_x, staged_y, car):
        return (staged_x, staged_y, heading)

    if _is_on_surface(track, bx, by) and not _car_overlaps_any(sim_cars, idx, bx, by, car):
        return (bx, by, heading)

    for ring in range(1, 10):
        radius = ring * 18.0
        for step in range(16):
            angle = (step / 16.0) * math.tau
            px = bx + math.cos(angle) * radius
            py = by + math.sin(angle) * radius
            if not _is_on_surface(track, px, py):
                continue
            if _car_overlaps_any(sim_cars, idx, px, py, car):
                continue
            return (px, py, heading)
    return (bx, by, heading)


def _optimize_start_grid(track, sim_cars: list[SimCar]) -> int:
    """Re-arrange all loaded cars into a tidy starting grid aligned with the
    racing direction at the start grid zone. Each positioned car's state and
    start_pose are updated and its waypoints rebuilt from the new placement.

    Returns the number of cars positioned.
    """
    if track is None or not sim_cars:
        return 0

    x, y, w, h = track.start_grid
    spawn_x = x + w / 2
    spawn_y = y + h / 2
    heading = _heading_from_track_position(track, spawn_x, spawn_y)
    fwd_x = math.cos(heading)
    fwd_y = math.sin(heading)
    side_x = -fwd_y
    side_y = fwd_x

    lanes_per_row = 3
    lane_slots = (0, -1, 1)
    positioned = 0
    for idx, entry in enumerate(sim_cars):
        car = entry.config
        lane = lane_slots[idx % lanes_per_row]
        row = idx // lanes_per_row
        forward_spacing = max(car.length * 1.7, 86.0)
        lateral_spacing = max(car.width * 1.35, 34.0)
        gx = spawn_x - fwd_x * (row * forward_spacing) + side_x * (lane * lateral_spacing)
        gy = spawn_y - fwd_y * (row * forward_spacing) + side_y * (lane * lateral_spacing)
        if not _is_on_surface(track, gx, gy) or _car_overlaps_any(sim_cars, idx, gx, gy, car):
            gx, gy, heading = _find_open_pose(track, sim_cars, idx, car, (gx, gy, heading))

        entry.state.x = gx
        entry.state.y = gy
        entry.state.heading_radians = heading
        entry.start_pose = (gx, gy, heading)
        _rebuild_route_for_pose(track, entry.route_plan, entry.start_pose)
        positioned += 1

    return positioned



def _resolve_car_overlaps(sim_cars: list[SimCar]) -> None:
    for i in range(len(sim_cars)):
        a = sim_cars[i]
        for j in range(i + 1, len(sim_cars)):
            b = sim_cars[j]
            ra = _car_collision_radius(a.config)
            rb = _car_collision_radius(b.config)
            min_dist = ra + rb
            dx = b.state.x - a.state.x
            dy = b.state.y - a.state.y
            dist_sq = dx * dx + dy * dy
            if dist_sq >= min_dist * min_dist:
                continue

            dist = math.sqrt(dist_sq) if dist_sq > 1e-6 else 1e-3
            nx = dx / dist
            ny = dy / dist
            overlap = min_dist - dist
            push = overlap * 0.5 + 0.5

            a.state.x -= nx * push
            a.state.y -= ny * push
            b.state.x += nx * push
            b.state.y += ny * push

            a.state.speed *= 0.92
            b.state.speed *= 0.92
            a.state.damage = min(100.0, a.state.damage + 0.3)
            b.state.damage = min(100.0, b.state.damage + 0.3)
            # Track last car-to-car contact for race stats.
            a.last_contact_time = a.race_elapsed
            a.last_contact_partner = b.instance_name
            b.last_contact_time = b.race_elapsed
            b.last_contact_partner = a.instance_name


def _serialize_car_starts(sim_cars: list[SimCar]) -> list[dict[str, object]]:
    return [
        {
            "instance_name": entry.instance_name,
            "car_file": entry.source_file,
            "start_pose": [entry.start_pose[0], entry.start_pose[1], entry.start_pose[2]],
        }
        for entry in sim_cars
    ]


def _finalize_race_outcome(sim_car: SimCar) -> None:
    avg_speed = 0.0
    if sim_car.speed_samples > 0:
        avg_speed = sim_car.speed_accum / sim_car.speed_samples
    outcome = CarRaceOutcome(
        race_time_seconds=sim_car.race_elapsed,
        best_lap_seconds=sim_car.best_lap_seconds,
        avg_speed=avg_speed,
        damage_taken=sim_car.state.damage,
        barrier_hits=sim_car.barrier_hits,
        completed=sim_car.state.laps > 0 and sim_car.state.state != "crashed",
    )
    sim_car.memory.remember(outcome)
    sim_car.learning.adapt(sim_car.behavior, sim_car.memory)


def _award_series_points(sim_cars: list[SimCar], race_number: int) -> None:
    """Award series points based on race finishing order.

    Cars that completed the configured lap limit are ranked by fastest completion
    time (smallest finish_time first). Cars that did not complete the lap limit
    are ranked behind them by laps completed, then distance traveled.
    """
    completers = [entry for entry in sim_cars if entry.completed_lap_limit]
    non_completers = [entry for entry in sim_cars if not entry.completed_lap_limit]

    ordered = sorted(completers, key=lambda entry: entry.finish_time) + sorted(
        non_completers,
        key=lambda entry: (entry.state.laps, entry.state.distance_traveled),
        reverse=True,
    )
    points_table = [5, 4, 3, 2]  # 1st-4th
    for rank, entry in enumerate(ordered):
        if rank < len(points_table):
            entry.series_stats.add_points(points_table[rank])
        elif entry.completed_lap_limit:
            # 1 point for completing the race (finishing the configured lap count).
            entry.series_stats.add_points(1)
        entry.series_stats.races_completed += 1
        # Track series fastest/slowest laps.
        if entry.best_lap_seconds > 0.0:
            record = SeriesLapRecord(
                car_name=entry.instance_name,
                race_number=race_number,
                lap_time=entry.best_lap_seconds,
                lap_number=entry.state.laps,
            )
            entry.series_stats.consider_lap(record)


def _learning_path(logs_dir: Path) -> Path:
    return logs_dir / "car_learning.json"


def _save_car_learning(sim_cars: list[SimCar], logs_dir: Path) -> None:
    """Persist each car's trained CarLearningState keyed by instance name."""
    try:
        import json
        logs_dir.mkdir(parents=True, exist_ok=True)
        data = {}
        for entry in sim_cars:
            data[entry.instance_name] = {
                "target_speed_bias": entry.learning.target_speed_bias,
                "steering_aggression": entry.learning.steering_aggression,
                "safety_bias": entry.learning.safety_bias,
            }
        with _learning_path(logs_dir).open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except Exception:
        pass


def _load_car_learning(instance_name: str, logs_dir: Path) -> dict[str, float] | None:
    """Load a saved learning state for a car, keyed by instance name (if present)."""
    try:
        import json
        with _learning_path(logs_dir).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        entry = data.get(instance_name)
        if not isinstance(entry, dict):
            return None
        return {
            "target_speed_bias": max(0.72, min(1.45, float(entry.get("target_speed_bias", 1.0)))),
            "steering_aggression": max(0.75, min(1.35, float(entry.get("steering_aggression", 1.0)))),
            "safety_bias": max(0.7, min(1.6, float(entry.get("safety_bias", 1.0)))),
        }
    except Exception:
        return None


def _reload_car_configs(sim_cars: list[SimCar], cars_dir: Path) -> None:
    """Hot-reload car configs from disk while preserving instance names."""
    for entry in sim_cars:
        car_path = cars_dir / entry.source_file
        if not car_path.exists():
            continue
        try:
            reloaded = load_car(car_path)
            entry.config = reloaded
        except Exception:
            continue


def _draw_car_stats_dropdown(
    screen: pygame.Surface,
    sim_cars: list[SimCar],
    stats_view_index: int,
    stats_dropdown_open: bool,
    stats_dropdown_scroll: int,
    cs_x: int,
    ss1_y: int,
    cs_w: int,
    bottom_pane: pygame.Rect,
    mouse_pos: tuple[int, int],
) -> tuple[pygame.Rect | None, list[tuple[int, pygame.Rect]], int]:
    """Draw the car selector dropdown button and (when open) its scrollable list.

    Returns (dropdown_rect, rows, clamped_scroll). Rows store the real car index
    so clicks map correctly even when the list is scrolled.
    """
    if not sim_cars:
        return None, [], 0

    stats_index = stats_view_index if stats_view_index < len(sim_cars) else 0
    selected = sim_cars[stats_index]

    # Dropdown button.
    sel_label = f"{selected.instance_name[:22]}  [v]"
    dropdown_rect = pygame.Rect(cs_x, ss1_y, min(cs_w, 200), 24)
    pygame.draw.rect(screen, (45, 52, 64), dropdown_rect, border_radius=4)
    pygame.draw.rect(screen, (95, 106, 126), dropdown_rect, width=1, border_radius=4)
    sel_surface = render_text_fit(sel_label, dropdown_rect.width - 12, 20, (235, 235, 235), start_size=18, min_size=10)
    screen.blit(sel_surface, (dropdown_rect.x + 6, dropdown_rect.y + 3))

    if not stats_dropdown_open:
        return dropdown_rect, [], 0

    # Open list: compute how many rows fit between the button and the bottom
    # pane edge (reserving space for the status message line).
    row_h = 22
    row_gap = 23
    available = bottom_pane.bottom - dropdown_rect.bottom - 4 - 44
    max_visible = max(1, min(len(sim_cars), available // row_gap))
    total_rows = len(sim_cars)
    max_scroll = max(0, total_rows - max_visible)
    scroll = max(0, min(stats_dropdown_scroll, max_scroll))

    # Opaque panel behind the list so underlying stats do not show through.
    panel_h = max_visible * row_gap + 4
    panel = pygame.Rect(dropdown_rect.x, dropdown_rect.bottom + 2, dropdown_rect.width, panel_h)
    pygame.draw.rect(screen, (45, 52, 64), panel)
    pygame.draw.rect(screen, (95, 106, 126), panel, width=1, border_radius=4)

    has_scrollbar = total_rows > max_visible
    row_w = panel.width - 2 - (10 if has_scrollbar else 0)

    rows: list[tuple[int, pygame.Rect]] = []
    drop_y = panel.y + 2
    for drop_idx in range(max_visible):
        car_idx = scroll + drop_idx
        if car_idx >= total_rows:
            break
        drop_entry = sim_cars[car_idx]
        drop_rect = pygame.Rect(panel.x + 1, drop_y, row_w, row_h)
        hovered = mouse_pos is not None and drop_rect.collidepoint(mouse_pos)
        pygame.draw.rect(screen, (60, 70, 86) if hovered else (45, 52, 64), drop_rect)
        drop_surface = render_text_fit(drop_entry.instance_name[:22], drop_rect.width - 10, 18, (235, 235, 235), start_size=16, min_size=10)
        screen.blit(drop_surface, (drop_rect.x + 5, drop_rect.y + 2))
        rows.append((car_idx, drop_rect))
        drop_y += row_gap

    # Scrollbar track + thumb when the list overflows.
    if has_scrollbar:
        bar_x = panel.right - 8
        track_rect = pygame.Rect(bar_x, panel.y + 2, 6, panel.height - 4)
        pygame.draw.rect(screen, (30, 36, 46), track_rect, border_radius=3)
        thumb_h = max(14, int(track_rect.height * max_visible / total_rows))
        thumb_y = track_rect.y
        if max_scroll > 0:
            thumb_y += int((track_rect.height - thumb_h) * scroll / max_scroll)
        thumb_rect = pygame.Rect(track_rect.x, thumb_y, 6, thumb_h)
        pygame.draw.rect(screen, (120, 132, 150), thumb_rect, border_radius=3)

    return dropdown_rect, rows, scroll


def main() -> int:
    project_dir = Path(__file__).resolve().parents[2]
    conf = read_simple_conf(
        project_dir / "etc" / "tracksim.conf",
        {
            "window_width": "1280",
            "window_height": "720",
            "capture_width": "960",
            "capture_height": "540",
            "render_fps": "60",
            "render_fps_streaming": "30",
            "tracks_dir": "tracks",
            "cars_dir": "cars",
            "default_track": "",
            "training_races": "10",
            "sim_steps_cap": "8000",
        },
    )

    width = as_int(conf, "window_width", 1280)
    height = as_int(conf, "window_height", 720)
    capture_width = max(2, as_int(conf, "capture_width", 640))
    capture_height = max(2, as_int(conf, "capture_height", 360))
    training_race_target = max(1, as_int(conf, "training_races", 10))
    sim_steps_cap = max(1, as_int(conf, "sim_steps_cap", 8000))
    tracks_dir = project_dir / conf.get("tracks_dir", "tracks")
    cars_dir = project_dir / conf.get("cars_dir", "cars")
    logs_dir = project_dir / "logs"

    # Headless streaming build (ASR_STREAM=1): cut render CPU load while the
    # 6-7fps stream is pushed to YouTube.
    #
    #  * Resolution: render AT the capture size (capture_* = the streaming frame
    #    size) instead of the interactive window_* size -> ~56% fewer pixels to
    #    draw. because capture == render here, the per-frame smoothscale
    #    downscale below is skipped entirely; ffmpeg (asr-stream-run) reads
    #    capture_* straight from tracksim.conf and upscales to 720p.
    #  * IMPORTANT: the frame size written to the FIFO must EXACTLY match what
    #    asr-stream-run derives from tracksim.conf (capture_width/height). They
    #    share no in-band framing, so any mismatch causes rawvideo desync ->
    #    a doubled/garbled stream. Keep capture_* as the single source of truth.
    #  * Frame rate: cap the render loop at streaming tick rate. Real-time
    #    physics only needs >=20fps (frame_dt is capped at 0.05s, sim_steps=1 in
    #    non-training mode), so 30fps keeps sim time == wall time. The stream
    #    itself is paced independently by FrameStreamer (ASR_FPS), so the
    #    6fps output is unaffected.
    asr_streaming = os.environ.get("ASR_STREAM") == "1"
    # stream_show_panes (tracksim.conf): when set AND ASR_STREAM=1, the stream is the
    # fullscreen track plus a cached leaderboard/bottom-stats overlay instead of a pure
    # fullscreen track. The overlay is re-rendered only when the standings change, so
    # enabling it costs almost nothing per frame.
    _sp = as_str(conf, "stream_show_panes", "0").strip().lower()
    stream_show_panes = _sp in ("1", "true", "yes", "on")
    render_fps = max(1, as_int(conf, "render_fps", 60))
    render_fps_streaming = max(20, as_int(conf, "render_fps_streaming", 30))
    if asr_streaming:
        # Render directly at the capture/streaming size (DO NOT change
        # capture_width/height here - asr-stream-run must read the same value).
        width = capture_width
        height = capture_height
        try:
            _fps_override = int(float(os.environ.get("ASR_RENDER_FPS", "0") or "0"))
        except ValueError:
            _fps_override = 0
        render_target = max(20, _fps_override or render_fps_streaming)
    else:
        render_target = max(1, render_fps)

    # Series configuration.
    series_name = as_str(conf, "series_name", "")
    series_race_target = max(0, as_int(conf, "series_races", 0))
    series_lap_limit = max(1, as_int(conf, "series_laps", 3))
    series_logo_path = as_str(conf, "series_logo", "")
    series_active = False
    series_completed_races = 0
    series_race_number = 0
    infinite_mode = False

    pygame.init()
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("Track Simulation")
    clock = pygame.time.Clock()
    font = create_default_font(22)
    crash_overlay = _load_crash_overlay(project_dir)
    series_logo = _load_series_logo(project_dir, series_logo_path)
    # Scale the series logo once up front (the panes draw it every frame; the old
    # per-frame smoothscale was pure wasted CPU on the Pi).
    series_logo_scaled = pygame.transform.smoothscale(series_logo, (80, 80)) if series_logo is not None else None

    # Optional headless streaming (ASR_STREAM=1): pipe rendered frames to
    # FFmpeg via a named FIFO. Only active when explicitly requested.
    # ASR_FPS (default 0 = no pacing) throttles delivery to a constant rate so
    # ffmpeg reads at TRACK_FPS and the stream advances in real time.
    streamer: FrameStreamer | None = None
    if asr_streaming:
        try:
            _track_fps = float(os.environ.get("ASR_FPS", "0") or "0")
        except ValueError:
            _track_fps = 0.0
        streamer = FrameStreamer(resolve_fifo_path(), fps=_track_fps)
        streamer.start()

    # Pane layout constants (matches RaceSimLayout.drawio.png).
    menu_bar_h = 34
    bottom_stats_h = 220
    leaderboard_w = 280
    if asr_streaming and stream_show_panes:
        # Scaled pane layout for the capture size: the track gets its own pane and
        # the leaderboard + bottom-stats HUD sit beside it (NEVER over the race).
        _lb_w = max(110, int(280 * width / 1600))
        _bs_h = max(92, int(220 * height / 900))
        track_pane = pygame.Rect(0, 0, width - _lb_w, height - _bs_h)
        leaderboard_pane = pygame.Rect(width - _lb_w, 0, _lb_w, height - _bs_h)
        bottom_pane = pygame.Rect(0, height - _bs_h, width, _bs_h)
    elif asr_streaming:
        # Pure fullscreen track view for the stream.
        track_pane = pygame.Rect(0, 0, width, height)
        leaderboard_pane = pygame.Rect(0, 0, 0, 0)
        bottom_pane = pygame.Rect(0, 0, 0, 0)
    else:
        track_pane = pygame.Rect(0, menu_bar_h, width - leaderboard_w, height - menu_bar_h - bottom_stats_h)
        leaderboard_pane = pygame.Rect(width - leaderboard_w, menu_bar_h, leaderboard_w, height - menu_bar_h - bottom_stats_h)
        bottom_pane = pygame.Rect(0, height - bottom_stats_h, width, bottom_stats_h)

    track = None
    current_track_path: Path | None = None
    sim_cars: list[SimCar] = []
    selected_car_index: int | None = None
    racing = False
    paused = False
    race_outcome_saved = True
    decision_logger: RaceDecisionLogger | None = None
    waypoint_density_step = 3
    message = "L load track, C load car, N start/reset race, Del remove selected car, Q quit"
    open_menu: str | None = None
    item_rects: dict[tuple[str, str], pygame.Rect] = {}
    # Reassigned each frame by draw_dropdown_menus (not in ASR_STREAM mode).
    header_rects: dict[tuple[str, str], pygame.Rect] = {}
    load_picker_open = False
    load_picker_kind = "track"
    load_picker_files: list[Path] = []
    load_picker_rows: list[tuple[int, pygame.Rect]] = []

    show_bottom_car_stats = True
    show_bottom_race_stats = True
    stats_dropdown_open = False
    stats_dropdown_rows: list[tuple[int, pygame.Rect]] = []
    stats_view_index = 0
    stats_dropdown_rect: pygame.Rect | None = None
    stats_dropdown_scroll = 0
    training_active = False
    training_total_races = training_race_target
    training_completed_races = 0
    training_speed_multiplier = max(1, as_int(conf, "simulation_speed", 100))

    dragging_index: int | None = None
    drag_offset = (0.0, 0.0)
    _next_car_number = 1

    menus = [
        ("Start", ["Load Track", "Load Car", "Remove Selected Car", "Optimize", "Save Track", "Quit"]),
        ("Race", ["Start Race", "Simulate", "Start Series", "Infinite Mode", "Pause/Resume", "Reset Cars", "+ Waypoint", "- Waypoint", "Quit Race"]),
        ("Stats", ["Toggle Car Stats", "Toggle Race Stats"]),
    ]

    def remove_selected_car() -> None:
        nonlocal selected_car_index, message
        if selected_car_index is None or selected_car_index < 0 or selected_car_index >= len(sim_cars):
            message = "No selected car to remove."
            return
        removed_name = sim_cars[selected_car_index].instance_name
        sim_cars.pop(selected_car_index)
        if not sim_cars:
            selected_car_index = None
        else:
            selected_car_index = min(selected_car_index, len(sim_cars) - 1)
        message = f"Removed car {removed_name}."

    def _personalize_route(route_plan: CarRoutePlan, car_index: int) -> None:
        """Apply a per-car lateral offset so fresh/generated routes are unique."""
        if len(route_plan.permanent_waypoints) < 3:
            return
        lane_slot = (car_index % 3) - 1
        offset = lane_slot * 14.0
        if abs(offset) < 1e-4:
            return
        n = len(route_plan.permanent_waypoints)
        pts: list[tuple[float, float]] = []
        for i, wp in enumerate(route_plan.permanent_waypoints):
            prev_wp = route_plan.permanent_waypoints[(i - 1) % n]
            next_wp = route_plan.permanent_waypoints[(i + 1) % n]
            tan_x = next_wp.x - prev_wp.x
            tan_y = next_wp.y - prev_wp.y
            tan_len = math.hypot(tan_x, tan_y)
            if tan_len <= 1e-6:
                pts.append((wp.x, wp.y))
                continue
            normal_x = -(tan_y / tan_len)
            normal_y = tan_x / tan_len
            pts.append((wp.x + normal_x * offset, wp.y + normal_y * offset))
        route_plan.permanent_waypoints = [
            Waypoint(x=px, y=py, kind="permanent", source="generated")
            for px, py in pts
        ]
        _normalize_route_order(track, route_plan)

    def add_loaded_car(car_path: Path, instance_name: str | None = None, start_pose: tuple[float, float, float] | None = None) -> None:
        nonlocal selected_car_index, _next_car_number
        if track is None:
            return
        loaded = load_car(car_path)
        name = _make_unique_car_name(instance_name or loaded.name, sim_cars)
        profile, learning, pass_side_bias, pace_bias, steer_bias = _build_personality(name, loaded)
        # Restore persisted learning from prior training sessions (if any).
        saved_learning = _load_car_learning(name, logs_dir)
        if saved_learning is not None:
            learning.target_speed_bias = saved_learning["target_speed_bias"]
            learning.steering_aggression = saved_learning["steering_aggression"]
            learning.safety_bias = saved_learning["safety_bias"]
        route_plan = _load_route_plan_from_track(track, name)
        metadata = track.metadata if isinstance(track.metadata, dict) else {}
        raw_routes = metadata.get("car_routes", {})
        raw_entry = raw_routes.get(name) if isinstance(raw_routes, dict) else None
        raw_list = raw_entry if isinstance(raw_entry, list) else (raw_entry.get("waypoints") if isinstance(raw_entry, dict) else None)
        stored_all_generated = isinstance(raw_list, list) and raw_list and all(
            isinstance(item, dict) and item.get("source") == "generated"
            for item in raw_list
        )
        # Personalize fresh routes (no stored route, or stored all-generated routes).
        if not raw_list or stored_all_generated:
            _personalize_route(route_plan, len(sim_cars))
        state = spawn_state(track, loaded)
        pose = (state.x, state.y, state.heading_radians)
        has_valid_explicit_start = False
        if start_pose is not None:
            x, y, heading = start_pose
            if _is_on_surface(track, x, y):
                state.x = x
                state.y = y
                state.heading_radians = heading
                pose = (x, y, heading)
            has_valid_explicit_start = True
        car_number = _next_car_number
        _next_car_number += 1
        sim_cars.append(
            SimCar(
                instance_name=name,
                source_file=car_path.name,
                config=loaded,
                state=state,
                start_pose=pose,
                behavior=profile,
                learning=learning,
                pass_side_bias=pass_side_bias,
                pace_bias=pace_bias,
                steer_bias=steer_bias,
                route_plan=route_plan,
                vision_matrix=VisionMatrix.empty(),
                car_number=car_number,
            )
        )
        inserted_index = len(sim_cars) - 1
        if has_valid_explicit_start and not _car_overlaps_any(sim_cars, inserted_index, pose[0], pose[1], loaded):
            open_pose = pose
        else:
            open_pose = _find_open_pose(track, sim_cars, inserted_index, loaded, pose)
        sim_cars[inserted_index].state.x = open_pose[0]
        sim_cars[inserted_index].state.y = open_pose[1]
        sim_cars[inserted_index].state.heading_radians = open_pose[2]
        sim_cars[inserted_index].start_pose = open_pose
        _apply_pose_offset_to_route(track, sim_cars[inserted_index].route_plan, open_pose)
        selected_car_index = len(sim_cars) - 1

    def load_track_into_session(chosen: Path, prefix: str = "Loaded track") -> None:
        nonlocal track, current_track_path, sim_cars, selected_car_index, message, _next_car_number
        loaded_track = load_track(chosen)
        loaded_track.prepare()
        track = loaded_track
        current_track_path = chosen
        sim_cars = []
        selected_car_index = None
        _next_car_number = 1

        car_entries = loaded_track.metadata.get("car_entries", {}) if isinstance(loaded_track.metadata, dict) else {}

        starts = loaded_track.metadata.get("car_starts", []) if isinstance(loaded_track.metadata, dict) else []
        for raw in starts:
            if not isinstance(raw, dict):
                continue
            car_file = str(raw.get("car_file", "")).strip()
            if not car_file:
                continue
            pose_raw = raw.get("start_pose", [])
            pose = None
            if isinstance(pose_raw, (list, tuple)) and len(pose_raw) >= 3:
                pose = (float(pose_raw[0]), float(pose_raw[1]), float(pose_raw[2]))
            instance_name = str(raw.get("instance_name", "")).strip() or None
            car_path = cars_dir / car_file
            if car_path.exists():
                add_loaded_car(car_path, instance_name=instance_name, start_pose=pose)
                # Apply any stored car_entries metadata (number, name, colors).
                if sim_cars:
                    latest = sim_cars[-1]
                    entry_key = str(latest.car_number)
                    stored = car_entries.get(entry_key, {})
                    if isinstance(stored, dict):
                        stored_name = str(stored.get("name", "")).strip() if "name" in stored else ""
                        if stored_name:
                            latest.instance_name = _make_unique_car_name(stored_name, sim_cars[:-1])
                        if "body_color" in stored and isinstance(stored["body_color"], (list, tuple)) and len(stored["body_color"]) >= 3:
                            latest.config.body_color = (int(stored["body_color"][0]), int(stored["body_color"][1]), int(stored["body_color"][2]))
                        if "nose_color" in stored and isinstance(stored["nose_color"], (list, tuple)) and len(stored["nose_color"]) >= 3:
                            latest.config.nose_color = (int(stored["nose_color"][0]), int(stored["nose_color"][1]), int(stored["nose_color"][2]))

        if not sim_cars:
            latest_car = load_latest(cars_dir, ".car")
            if latest_car is not None:
                add_loaded_car(latest_car)

        message = f"{prefix} {chosen.name}."

    def start_race_session(training: bool = False) -> bool:
        nonlocal racing, paused, race_outcome_saved, decision_logger, message
        if track is None:
            message = "Load a track before starting a race."
            return False
        if not sim_cars:
            message = "Load at least one car first."
            return False

        for entry in sim_cars:
            _reset_for_race(entry, track)
        racing = True
        paused = False
        race_outcome_saved = False
        decision_logger = RaceDecisionLogger.start(logs_dir, track.name, sim_cars)
        if training:
            message = f"Training race {training_completed_races + 1}/{training_total_races} in progress."
        else:
            message = "Race started for all loaded cars."
        return True

    default_track_name = conf.get("default_track", "").strip()
    if default_track_name:
        default_track_candidates: list[Path] = []
        default_track_path = Path(default_track_name)
        if default_track_path.is_absolute():
            default_track_candidates.append(default_track_path)
        else:
            default_track_candidates.append(tracks_dir / default_track_path)
            default_track_candidates.append(project_dir / default_track_path)

        chosen_default_track = next((candidate for candidate in default_track_candidates if candidate.exists()), None)
        if chosen_default_track is not None:
            load_track_into_session(chosen_default_track, prefix="Auto-loaded default track")
        else:
            message = f"Configured default_track not found: {default_track_name}."

    # Auto-start infinite mode when requested via env (TRACKSIM_INFINITE=1) or
    # CLI flag (--infinite). Reuses the exact code path of the "Race -> Infinite
    # Mode" menu action so the existing all-wreck reset/hot-reload loop takes over.
    infinite_requested = "--infinite" in sys.argv[1:] or os.environ.get("TRACKSIM_INFINITE") == "1"
    if infinite_requested:
        if track is None:
            message = "Infinite mode requested but no track is loaded."
        elif not sim_cars:
            message = "Infinite mode requested but no cars are loaded."
        else:
            for entry in sim_cars:
                entry.series_stats = CarSeriesStats()
            series_active = False
            infinite_mode = True
            series_completed_races = 0
            series_race_number = 0
            if not start_race_session(training=False):
                infinite_mode = False
            else:
                message = "Infinite mode started. Cars will auto-reset on all-wreck."

    # Auto-load above selects the most recently added car. For an unattended
    # stream, start with NO car selected (a highlighted car + vision overlays
    # aren't wanted on the live output).
    selected_car_index = None

    # Streaming chrome: cached, OPAQUE leaderboard + bottom-stats panes drawn beside
    # the track (track_pane is already shrunk to leave room), so they never overlap
    # the race. Re-rendered only when the standings change; blitted each frame.
    chrome_sig_prev = ""
    chrome_lb = None
    chrome_bottom = None
    if asr_streaming and stream_show_panes:
        chrome_lb = pygame.Surface((leaderboard_pane.width, leaderboard_pane.height))
        chrome_bottom = pygame.Surface((bottom_pane.width, bottom_pane.height))

    running = True
    while running:
        # Real-time frame dt (capped). Simulation speed is handled via sub-steps,
        # not by inflating dt beyond stable physics step sizes.
        frame_dt = min(clock.get_time() / 1000.0, 0.05)
        if training_active:
            # Run multiple fixed sub-steps per frame to accelerate simulation
            # without destabilizing physics/control. Each sub-step uses a capped
            # dt so lap counting and waypoint advancement remain accurate.
            sim_substep_dt = 0.02
            sim_steps = max(1, int(frame_dt * training_speed_multiplier / sim_substep_dt))
            sim_steps = min(sim_steps, sim_steps_cap)  # Bound CPU cost.
            dt = sim_substep_dt
        else:
            sim_steps = 1
            dt = frame_dt

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    running = False
                elif event.key == pygame.K_ESCAPE:
                    load_picker_open = False
                    open_menu = None
                elif event.key == pygame.K_l:
                    load_picker_kind = "track"
                    load_picker_files = sorted(tracks_dir.glob("*.track"))
                    load_picker_open = True
                    message = "Select a track file to load."
                elif event.key == pygame.K_c:
                    if track is None:
                        message = "Load a track before loading cars."
                    else:
                        load_picker_kind = "car"
                        load_picker_files = sorted(cars_dir.glob("*.car"))
                        load_picker_open = True
                        message = "Select a car file to load."
                elif event.key == pygame.K_n:
                    training_active = False
                    start_race_session(training=False)
                elif event.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
                    if racing:
                        message = "Pause race before removing cars."
                    else:
                        remove_selected_car()
            elif event.type == pygame.MOUSEMOTION:
                if dragging_index is not None and not racing and track is not None:
                    entry = sim_cars[dragging_index]
                    nx = event.pos[0] + drag_offset[0]
                    ny = event.pos[1] + drag_offset[1]
                    if _is_on_surface(track, nx, ny) and not _car_overlaps_any(sim_cars, dragging_index, nx, ny, entry.config):
                        entry.state.x = nx
                        entry.state.y = ny
                        entry.state.heading_radians = _heading_from_track_position(track, nx, ny)
                        entry.start_pose = (entry.state.x, entry.state.y, entry.state.heading_radians)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if dragging_index is not None and not racing and track is not None:
                    entry = sim_cars[dragging_index]
                    entry.start_pose = (entry.state.x, entry.state.y, entry.state.heading_radians)
                    _rebuild_route_for_pose(track, entry.route_plan, entry.start_pose)
                dragging_index = None
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                # Car stats dropdown interaction.
                if not training_active and show_bottom_car_stats and stats_dropdown_rect is not None and stats_dropdown_rect.collidepoint(event.pos):
                    stats_dropdown_open = not stats_dropdown_open
                    continue
                if not training_active and stats_dropdown_open:
                    chosen_stats = None
                    for idx, rect in stats_dropdown_rows:
                        if rect.collidepoint(event.pos):
                            chosen_stats = idx
                            break
                    stats_dropdown_open = False
                    if chosen_stats is not None and 0 <= chosen_stats < len(sim_cars):
                        stats_view_index = chosen_stats
                        selected_car_index = chosen_stats
                    continue

                if load_picker_open:
                    chosen_index = None
                    for idx, rect in load_picker_rows:
                        if rect.collidepoint(event.pos):
                            chosen_index = idx
                            break
                    if chosen_index is not None and chosen_index < len(load_picker_files):
                        chosen = load_picker_files[chosen_index]
                        if load_picker_kind == "car":
                            add_loaded_car(chosen)
                            message = f"Loaded car {sim_cars[-1].instance_name}."
                        else:
                            load_track_into_session(chosen)
                    load_picker_open = False
                    continue

                if track is not None and sim_cars:
                    clicked_car = False
                    for idx in range(len(sim_cars) - 1, -1, -1):
                        entry = sim_cars[idx]
                        car_rect = _car_draw_rect(entry.state, entry.config, track_transform)
                        if car_rect.collidepoint(event.pos):
                            selected_car_index = idx
                            stats_view_index = idx
                            if not racing:
                                dragging_index = idx
                                drag_offset = (entry.state.x - event.pos[0], entry.state.y - event.pos[1])
                            open_menu = None
                            clicked_car = True
                            break
                    if dragging_index is not None:
                        continue
                    if not clicked_car:
                        selected_car_index = None
                    else:
                        # Car click handled: skip menu/deselect logic so a car
                        # selected while racing is not immediately deselected.
                        continue

                action = menu_action_at(event.pos, header_rects, item_rects)
                if action is None:
                    open_menu = None
                    if not load_picker_open:
                        selected_car_index = None
                elif action.item == "":
                    open_menu = None if open_menu == action.menu else action.menu
                else:
                    open_menu = None
                    if action.menu == "Start" and action.item == "Quit":
                        running = False
                    elif action.menu == "Start" and action.item == "Load Track":
                        load_picker_kind = "track"
                        load_picker_files = sorted(tracks_dir.glob("*.track"))
                        load_picker_open = True
                        message = "Select a track file to load."
                    elif action.menu == "Start" and action.item == "Load Car":
                        if track is None:
                            message = "Load a track before loading cars."
                        else:
                            load_picker_kind = "car"
                            load_picker_files = sorted(cars_dir.glob("*.car"))
                            load_picker_open = True
                            message = "Select a car file to load."
                    elif action.menu == "Start" and action.item == "Remove Selected Car":
                        if racing:
                            message = "Pause race before removing cars."
                        else:
                            remove_selected_car()
                    elif action.menu == "Start" and action.item == "Optimize":
                        if track is None or not sim_cars:
                            message = "Load a track and at least one car first."
                        elif racing:
                            message = "Pause race before optimizing the start grid."
                        else:
                            count = _optimize_start_grid(track, sim_cars)
                            message = f"Start grid optimized for {count} cars."

                    elif action.menu == "Start" and action.item == "Save Track":
                        if track is None:
                            message = "Load a track before saving."
                        else:
                            if not isinstance(track.metadata, dict):
                                track.metadata = {}
                            track.metadata["car_starts"] = _serialize_car_starts(sim_cars)
                            track.metadata["car_routes"] = _serialize_car_routes(sim_cars)
                            track.metadata["car_entries"] = _serialize_car_entries(sim_cars)
                            if current_track_path is None:
                                safe_name = track.name.strip().replace(" ", "_") or "track"
                                current_track_path = tracks_dir / f"{safe_name}.track"
                            save_track(current_track_path, track)
                            message = f"Saved track {current_track_path.name} with {len(sim_cars)} cars."
                    elif action.menu == "Race" and action.item == "Start Race":
                        training_active = False
                        start_race_session(training=False)
                    elif action.menu == "Race" and action.item == "Simulate":
                        if racing:
                            message = "Quit current race before starting simulation."
                        else:
                            training_total_races = training_race_target
                            training_completed_races = 0
                            training_active = True
                            if not start_race_session(training=True):
                                training_active = False
                    elif action.menu == "Race" and action.item == "Start Series":
                        if racing:
                            message = "Quit current race before starting a series."
                        else:
                            # Reset series state and start a new series.
                            for entry in sim_cars:
                                entry.series_stats = CarSeriesStats()
                            series_active = True
                            infinite_mode = False
                            series_completed_races = 0
                            series_race_number = 0
                            if not start_race_session(training=False):
                                series_active = False
                            else:
                                message = f"Series started: {series_name or 'Unnamed'} ({series_race_target if series_race_target > 0 else '∞'} races)"
                    elif action.menu == "Race" and action.item == "Infinite Mode":
                        if racing:
                            message = "Quit current race before starting infinite mode."
                        else:
                            # Reset series state and start infinite mode.
                            for entry in sim_cars:
                                entry.series_stats = CarSeriesStats()
                            series_active = False
                            infinite_mode = True
                            series_completed_races = 0
                            series_race_number = 0
                            if not start_race_session(training=False):
                                infinite_mode = False
                            else:
                                message = "Infinite mode started. Cars will auto-reset on all-wreck."
                    elif action.menu == "Race" and action.item == "Pause/Resume":
                        if training_active:
                            message = "Pause/Resume disabled during simulation."
                            continue
                        if not sim_cars:
                            message = "Load at least one car first."
                        elif not racing:
                            message = "Start a race before pausing."
                        else:
                            paused = not paused
                            message = "Race resumed." if not paused else "Race paused."
                    elif action.menu == "Race" and action.item == "Reset Cars":
                        if not sim_cars:
                            message = "No cars loaded."
                        else:
                            for entry in sim_cars:
                                _reset_for_race(entry, track)
                            racing = False
                            paused = False
                            race_outcome_saved = True
                            message = "All cars reset to saved starting positions."
                    elif action.menu == "Race" and action.item == "+ Waypoint":
                        if track is None or not sim_cars:
                            message = "Load a track and at least one car first."
                        elif racing:
                            message = "Pause race before changing waypoint density."
                        else:
                            for entry in sim_cars:
                                _increase_permanent_waypoints(track, entry.route_plan, increment=waypoint_density_step)
                                _apply_pose_offset_to_route(track, entry.route_plan, entry.start_pose)
                            message = f"Increased waypoint density by {waypoint_density_step}."
                    elif action.menu == "Race" and action.item == "- Waypoint":
                        if track is None or not sim_cars:
                            message = "Load a track and at least one car first."
                        elif racing:
                            message = "Pause race before changing waypoint density."
                        else:
                            for entry in sim_cars:
                                _decrease_permanent_waypoints(track, entry.route_plan, decrement=waypoint_density_step)
                                _apply_pose_offset_to_route(track, entry.route_plan, entry.start_pose)
                            message = f"Decreased waypoint density by {waypoint_density_step}."
                    elif action.menu == "Race" and action.item == "Quit Race":
                        racing = False
                        paused = False
                        race_outcome_saved = False
                        training_active = False
                        training_completed_races = 0
                        message = "Race ended."
                    elif action.menu == "Stats" and action.item == "Toggle Car Stats":
                        show_bottom_car_stats = not show_bottom_car_stats
                        message = "Car stats shown." if show_bottom_car_stats else "Car stats hidden."
                    elif action.menu == "Stats" and action.item == "Toggle Race Stats":
                        show_bottom_race_stats = not show_bottom_race_stats
                        message = "Race stats shown." if show_bottom_race_stats else "Race stats hidden."
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button in (4, 5):
                if not training_active and stats_dropdown_open and stats_dropdown_rect is not None:
                    stats_dropdown_scroll += 1 if event.button == 5 else -1
            elif event.type == pygame.MOUSEWHEEL:
                if not training_active and stats_dropdown_open and stats_dropdown_rect is not None:
                    stats_dropdown_scroll -= event.y

        # Pane-based layout: dark background, track in its own pane, leaderboard
        # side pane, and bottom stats pane.
        screen.fill((18, 24, 32))

        if not training_active:
            # Draw track clipped to track pane.
            if track is not None:
                screen.set_clip(track_pane)
                # Fill track pane with grass color.
                pygame.draw.rect(screen, (42, 145, 75), track_pane)
                track_transform = _compute_track_view_transform(track, track_pane)
                draw_track(screen, track, track_transform)
                screen.set_clip(None)
            else:
                pygame.draw.rect(screen, (42, 145, 75), track_pane)
                track_transform = (1.0, 0.0, 0.0)

            if not asr_streaming:
                # Leaderboard side pane.
                pygame.draw.rect(screen, (24, 28, 34), leaderboard_pane)
                pygame.draw.rect(screen, (60, 70, 86), leaderboard_pane, width=1)
                draw_lines(screen, font, ["Leaderboard"], leaderboard_pane.x + 10, leaderboard_pane.y + 8, (235, 235, 235))
                if sim_cars:
                    ordered = sorted(
                        sim_cars,
                        key=lambda e: (e.state.laps, e.state.distance_traveled),
                        reverse=True,
                    )
                    lb_y = leaderboard_pane.y + 38
                    for pos, entry in enumerate(ordered[:12], start=1):
                        pts = entry.series_stats.points
                        cn = entry.car_number
                        car_label = f"#{cn} {entry.instance_name[:14]}" if cn > 0 else entry.instance_name[:16]
                        lb_line = f"{pos}. {car_label} L{entry.state.laps} {pts}p"
                        draw_lines_fit(
                            screen, [lb_line],
                            leaderboard_pane.x + 8, lb_y,
                            (235, 235, 235),
                            max_width=leaderboard_pane.width - 16,
                            line_height=22, start_size=18, min_size=10,
                        )
                        lb_y += 22

                # Bottom stats pane background.
                pygame.draw.rect(screen, (24, 28, 34), bottom_pane)
                pygame.draw.rect(screen, (60, 70, 86), bottom_pane, width=1)

                # Series stats 1 (left section of bottom pane).
                ss1_x = bottom_pane.x + 10
                ss1_y = bottom_pane.y + 8
                ss1_w = 300
                draw_lines(screen, font, ["Series Stats"], ss1_x, ss1_y, (200, 220, 255))
                series_lines = [
                    f"Name: {series_name}" if series_name else "Name: (none)",
                    f"Races: {series_completed_races}/{series_race_target if series_race_target > 0 else '∞'}",
                ]
                # Series fastest/slowest lap across all cars.
                all_fastest = [e.series_stats.fastest_lap for e in sim_cars if e.series_stats.fastest_lap]
                all_slowest = [e.series_stats.slowest_lap for e in sim_cars if e.series_stats.slowest_lap]
                if all_fastest:
                    fr = min(all_fastest, key=lambda r: r.lap_time)
                    series_lines.append(f"Fast: {fr.car_name[:12]} R{fr.race_number} {fr.lap_time:.2f}s")
                else:
                    series_lines.append("Fast: --")
                if all_slowest:
                    sr = max(all_slowest, key=lambda r: r.lap_time)
                    series_lines.append(f"Slow: {sr.car_name[:12]} R{sr.race_number} {sr.lap_time:.2f}s")
                else:
                    series_lines.append("Slow: --")
                draw_lines_fit(screen, series_lines, ss1_x, ss1_y + 28, (225, 225, 225), max_width=ss1_w, line_height=20, start_size=16, min_size=10)

                # Series logo in series stats 1.
                if series_logo_scaled is not None:
                    logo_size = 80
                    screen.blit(series_logo_scaled, (ss1_x + ss1_w - logo_size - 4, ss1_y + 4))

                # Series stats 2 (top 5 points leaders).
                ss2_x = ss1_x + ss1_w + 10
                ss2_w = 240
                draw_lines(screen, font, ["Points Leaders"], ss2_x, ss1_y, (200, 220, 255))
                if sim_cars:
                    leaders = sorted(sim_cars, key=lambda e: e.series_stats.points, reverse=True)[:5]
                    pl_y = ss1_y + 28
                    for rank, entry in enumerate(leaders, start=1):
                        cn = entry.car_number
                        car_label = f"#{cn} {entry.instance_name[:12]}" if cn > 0 else entry.instance_name[:14]
                        pl_line = f"{rank}. {car_label} - {entry.series_stats.points}pts"
                        draw_lines_fit(screen, [pl_line], ss2_x, pl_y, (225, 225, 225), max_width=ss2_w, line_height=20, start_size=16, min_size=10)
                        pl_y += 20

                # Race stats 1.
                if show_bottom_race_stats:
                    rs1_x = ss2_x + ss2_w + 10
                    rs1_w = 260
                    draw_lines(screen, font, ["Race Stats"], rs1_x, ss1_y, (200, 220, 255))
                    leader_entry = max(sim_cars, key=lambda e: (e.state.laps, e.state.distance_traveled)) if sim_cars else None
                    top_speed_entry = max(sim_cars, key=lambda e: e.max_race_speed) if sim_cars else None
                    leader_laps = max((e.state.laps for e in sim_cars), default=0)
                    best, worst = _best_worst_lap(sim_cars)
                    race1_lines = [
                        f"Leader: {leader_entry.instance_name[:16]}" if leader_entry else "Leader: --",
                        f"Top Spd: {top_speed_entry.instance_name[:12]} {top_speed_entry.max_race_speed:.0f}" if top_speed_entry else "Top Spd: --",
                        f"Laps: {leader_laps}",
                        f"Fast: {best[1]:.2f}s ({best[0][:10]})" if best else "Fast: --",
                        f"Slow: {worst[1]:.2f}s ({worst[0][:10]})" if worst else "Slow: --",
                    ]
                    draw_lines_fit(screen, race1_lines, rs1_x, ss1_y + 28, (225, 225, 225), max_width=rs1_w, line_height=20, start_size=16, min_size=10)

                    # Race stats 2 (drift, crash, contact).
                    rs2_x = rs1_x + rs1_w + 10
                    rs2_w = 280
                    draw_lines(screen, font, ["Race Stats 2"], rs2_x, ss1_y, (200, 220, 255))
                    drift_car = max(sim_cars, key=lambda e: e.max_drift_duration) if sim_cars else None
                    crash_car = min((e for e in sim_cars if e.first_crash_time is not None), key=lambda e: e.first_crash_time, default=None) if sim_cars else None
                    contact_car = max((e for e in sim_cars if e.last_contact_time is not None), key=lambda e: e.last_contact_time, default=None) if sim_cars else None
                    race2_lines = [
                        f"Longest Drift: {drift_car.instance_name[:12]} {drift_car.max_drift_duration:.1f}s" if drift_car and drift_car.max_drift_duration > 0 else "Longest Drift: --",
                        f"Quick Crash: {crash_car.instance_name[:12]} {crash_car.first_crash_time:.1f}s" if crash_car else "Quick Crash: --",
                        f"Last Contact: {contact_car.instance_name[:12]} {contact_car.last_contact_time:.1f}s" if contact_car else "Last Contact: --",
                    ]
                    draw_lines_fit(screen, race2_lines, rs2_x, ss1_y + 28, (225, 225, 225), max_width=rs2_w, line_height=20, start_size=16, min_size=10)

                    # Car stats pane (right section of bottom pane).
                    if show_bottom_car_stats and sim_cars:
                        cs_x = rs2_x + rs2_w + 10
                        cs_w = bottom_pane.right - cs_x - 10
                        if cs_w > 100:
                            stats_index = stats_view_index if stats_view_index < len(sim_cars) else 0
                            selected = sim_cars[stats_index]
                            cn = selected.car_number
                            car_header = f"Car #{cn}: {selected.instance_name[:16]}" if cn > 0 else f"Car: {selected.instance_name[:18]}"
                            draw_lines(screen, font, [car_header], cs_x, ss1_y + 30, (200, 220, 255))
                            car_lines = [
                                f"State: {selected.state.state}",
                                f"Speed: {selected.state.speed:.1f}",
                                f"Fuel: {selected.state.fuel:.1f}",
                                f"Tire: {selected.state.tire_health:.1f}",
                                f"Damage: {selected.state.damage:.1f}",
                                f"Laps: {selected.state.laps}",
                                f"Best: {selected.best_lap_seconds:.2f}s" if selected.best_lap_seconds > 0 else "Best: --",
                            ]
                            draw_lines_fit(screen, car_lines, cs_x, ss1_y + 58, (225, 225, 225), max_width=cs_w, line_height=20, start_size=16, min_size=10)
                            # Draw the dropdown last so its opaque panel covers the
                            # car stats text beneath it.
                            stats_dropdown_rect, stats_dropdown_rows, stats_dropdown_scroll = _draw_car_stats_dropdown(
                                screen,
                                sim_cars,
                                stats_view_index,
                                stats_dropdown_open,
                                stats_dropdown_scroll,
                                cs_x,
                                ss1_y,
                                cs_w,
                                bottom_pane,
                                pygame.mouse.get_pos(),
                            )
                else:
                    # Render car stats without race stats - repositioned
                    if show_bottom_car_stats and sim_cars:
                        cs_x = ss2_x + ss2_w + 10
                        cs_w = bottom_pane.right - cs_x - 10
                        if cs_w > 100:
                            stats_index = stats_view_index if stats_view_index < len(sim_cars) else 0
                            selected = sim_cars[stats_index]
                            cn = selected.car_number
                            car_header = f"Car #{cn}: {selected.instance_name[:16]}" if cn > 0 else f"Car: {selected.instance_name[:18]}"
                            draw_lines(screen, font, [car_header], cs_x, ss1_y + 30, (200, 220, 255))
                            car_lines = [
                                f"State: {selected.state.state}",
                                f"Speed: {selected.state.speed:.1f}",
                                f"Fuel: {selected.state.fuel:.1f}",
                                f"Tire: {selected.state.tire_health:.1f}",
                                f"Damage: {selected.state.damage:.1f}",
                                f"Laps: {selected.state.laps}",
                                f"Best: {selected.best_lap_seconds:.2f}s" if selected.best_lap_seconds > 0 else "Best: --",
                            ]
                            draw_lines_fit(screen, car_lines, cs_x, ss1_y + 58, (225, 225, 225), max_width=cs_w, line_height=20, start_size=16, min_size=10)
                            # Draw the dropdown last so its opaque panel covers the
                            # car stats text beneath it.
                            stats_dropdown_rect, stats_dropdown_rows, stats_dropdown_scroll = _draw_car_stats_dropdown(
                                screen,
                                sim_cars,
                                stats_view_index,
                                stats_dropdown_open,
                                stats_dropdown_scroll,
                                cs_x,
                                ss1_y,
                                cs_w,
                                bottom_pane,
                                pygame.mouse.get_pos(),
                            )

        if racing and not paused and track is not None and sim_cars:
            all_stopped = True
            for idx, entry in enumerate(sim_cars):
                state = entry.state
                car = entry.config
                # Leader line-offset freeze removed: all cars continue adapting.
                entry.line_offset_frozen = False
                if state.state != "crashed":
                    all_stopped = False

                traffic = [
                    (
                        other.state.x,
                        other.state.y,
                        _car_collision_radius(other.config),
                        other.state.speed,
                        other.state.state == "crashed",
                        (other.state.state != "crashed" and abs(other.state.speed) < 2.0),
                    )
                    for j, other in enumerate(sim_cars)
                    if j != idx
                ]

                entry.vision_matrix = _build_vision_matrix(state, car, track, entry.route_plan, traffic)

                if state.state == "crashed":
                    # Freeze route/autonomy evolution once a car has crashed.
                    entry.route_stall_recover_time = 0.0
                    entry.hard_route_stall_time = 0.0
                    entry.hard_route_recenter_time = 0.0
                    continue

                # Fast handoff: if a permanent waypoint is already behind the car,
                # advance immediately so control logic does not brake to chase it.
                active_pre_wp = entry.route_plan.active_waypoint()
                if active_pre_wp is not None and len(entry.route_plan.permanent_waypoints) > 1:
                    fwd_x = math.cos(state.heading_radians)
                    fwd_y = math.sin(state.heading_radians)
                    pre_dx = active_pre_wp.x - state.x
                    pre_dy = active_pre_wp.y - state.y
                    pre_dist = math.hypot(pre_dx, pre_dy)
                    pre_ahead = pre_dx * fwd_x + pre_dy * fwd_y
                    if (
                        pre_ahead < -6.0
                        and pre_dist > max(20.0, car.length * 0.35)
                        and _car_is_on_racing_surface(state, car, track)
                        and state.wall_contact_frames == 0
                    ):
                        entry.route_plan.active_target_index = (
                            entry.route_plan.active_target_index + 1
                        ) % len(entry.route_plan.permanent_waypoints)
                        next_wp = entry.route_plan.active_waypoint()
                        if next_wp is not None:
                            entry.route_last_dist = math.hypot(next_wp.x - state.x, next_wp.y - state.y)
                        else:
                            entry.route_last_dist = float("inf")
                        entry.route_last_idx = entry.route_plan.active_target_index
                        entry.route_stall_time = 0.0
                        entry.route_idx_stall_time = 0.0
                        entry.route_stall_recover_time = max(entry.route_stall_recover_time, 0.8)
                        entry.waypoint_behind_time = 0.0

                throttle, brake, steering, brake_reason, coast_reason, seen_line_point = autonomous_controls(
                    state=state,
                    car=car,
                    track=track,
                    race_elapsed=entry.race_elapsed,
                    behavior=entry.behavior,
                    learning=entry.learning,
                    traffic=traffic,
                    pass_side_bias=entry.pass_side_bias,
                    pace_bias=entry.pace_bias,
                    steer_bias=entry.steer_bias,
                    route_plan=entry.route_plan,
                    vision_matrix=entry.vision_matrix,
                    last_visible_line_point=entry.last_visible_line_point,
                    preferred_line_offset=entry.preferred_line_offset,
                    stall_recover=entry.route_stall_recover_time > 0.0,
                    hard_recenter=entry.hard_route_recenter_time > 0.0,
                    post_waypoint_boost=entry.post_waypoint_boost_time,
                )
                entry.last_visible_line_point = seen_line_point
                if decision_logger is not None:
                    if brake > 0.05:
                        reason = brake_reason or "unspecified"
                        decision_logger.log_decision(entry.race_elapsed, entry.instance_name, "braking", reason, state.speed)
                    elif coast_reason:
                        decision_logger.log_decision(entry.race_elapsed, entry.instance_name, "coasting", coast_reason, state.speed)

                prev_laps = state.laps
                prev_wall_contact = state.wall_contact_frames
                prev_state_name = state.state

                # Apply physics in fixed sub-steps so accelerated simulation
                # remains stable. AI controls are computed once per frame and
                # held constant across sub-steps.
                physics_dt = 0.02
                physics_steps = max(1, int(dt / physics_dt))
                physics_steps = min(physics_steps, 250)
                actual_physics_dt = dt / physics_steps if physics_steps > 0 else dt
                for _phys_step in range(physics_steps):
                    on_surface = update_car_state(state, car, track, actual_physics_dt, throttle, brake, steering)
                    in_start_zone = pygame.Rect(track.start_grid).collidepoint(state.x, state.y)
                    lap0_wrap_allowed = in_start_zone and state.left_start_zone
                    allow_route_wrap = state.laps > 0 or lap0_wrap_allowed
                    update_lap_counter(state, track)
                    # Require full footprint on racing surface before advancing route index.
                    can_advance_waypoint = on_surface and state.wall_contact_frames == 0
                    if can_advance_waypoint:
                        if entry.route_plan.advance_if_reached(
                            state.x,
                            state.y,
                            threshold=max(40.0, car.length * 0.9),
                            allow_wrap=allow_route_wrap,
                        ):
                            entry.post_waypoint_boost_time = 1.3
                if entry.post_waypoint_boost_time <= 0.0 or not can_advance_waypoint:
                    entry.post_waypoint_boost_time = max(0.0, entry.post_waypoint_boost_time - dt)

                active_wp = entry.route_plan.active_waypoint()
                if active_wp is not None:
                    wp_dist = math.hypot(active_wp.x - state.x, active_wp.y - state.y)
                    idx = entry.route_plan.active_target_index
                    if idx == entry.route_last_idx:
                        entry.route_idx_stall_time += dt
                    else:
                        entry.route_idx_stall_time = 0.0
                    progressed = idx != entry.route_last_idx or wp_dist < entry.route_last_dist - 4.0
                    if progressed:
                        entry.route_stall_time = 0.0
                    else:
                        entry.route_stall_time += dt
                    entry.route_last_idx = idx
                    entry.route_last_dist = wp_dist

                    speed_sign = 1 if state.speed > 1.5 else (-1 if state.speed < -1.5 else 0)
                    if speed_sign != 0 and entry.last_speed_sign != 0 and speed_sign != entry.last_speed_sign:
                        entry.speed_flip_stall_time += dt
                    else:
                        entry.speed_flip_stall_time = max(0.0, entry.speed_flip_stall_time - dt * 0.5)
                    if speed_sign != 0:
                        entry.last_speed_sign = speed_sign

                    vision_center_state = entry.vision_matrix.get("center", "near").state
                    low_speed_deadlock = (
                        not progressed
                        and entry.route_stall_time > 1.2
                        and abs(state.speed) < 8.5
                    )
                    hard_stall_signal = (
                        not progressed
                        and (
                            vision_center_state == "barrier"
                            or entry.speed_flip_stall_time > 0.45
                            or low_speed_deadlock
                        )
                    )
                    if hard_stall_signal:
                        entry.hard_route_stall_time += dt
                    else:
                        entry.hard_route_stall_time = max(0.0, entry.hard_route_stall_time - dt)

                    # Skip guard: if the active permanent waypoint stays behind the car,
                    # advance once to prevent deadlock oscillation on straights.
                    if len(entry.route_plan.permanent_waypoints) > 1:
                        fwd_x = math.cos(state.heading_radians)
                        fwd_y = math.sin(state.heading_radians)
                        curr_ahead = (active_wp.x - state.x) * fwd_x + (active_wp.y - state.y) * fwd_y
                        if curr_ahead < -4.0 and wp_dist > max(20.0, car.length * 0.35):
                            entry.waypoint_behind_time += dt
                        else:
                            entry.waypoint_behind_time = max(0.0, entry.waypoint_behind_time - dt * 1.5)

                        if entry.waypoint_behind_time > 0.2:
                            entry.route_plan.active_target_index = (
                                entry.route_plan.active_target_index + 1
                            ) % len(entry.route_plan.permanent_waypoints)
                            next_wp = entry.route_plan.active_waypoint()
                            if next_wp is not None:
                                next_dist = math.hypot(next_wp.x - state.x, next_wp.y - state.y)
                                entry.route_last_dist = next_dist
                            else:
                                entry.route_last_dist = float("inf")
                            entry.route_last_idx = entry.route_plan.active_target_index
                            entry.route_stall_time = 0.0
                            entry.route_idx_stall_time = 0.0
                            entry.route_stall_recover_time = max(entry.route_stall_recover_time, 1.2)
                            entry.waypoint_behind_time = 0.0

                    if entry.route_stall_time > 2.4 and state.state != "crashed":
                        entry.route_stall_recover_time = max(entry.route_stall_recover_time, 1.0)
                        entry.route_stall_time = 0.0

                    # If the car remains very slow with clear forward vision for too long,
                    # advance one waypoint to break deadlock on stale/over-constrained targets.
                    if (
                        (entry.route_stall_time > 3.2 or entry.route_idx_stall_time > 4.6)
                        and vision_center_state == "clear"
                        and abs(state.speed) < 7.0
                        and len(entry.route_plan.permanent_waypoints) > 1
                        and _car_is_on_racing_surface(state, car, track)
                        and state.wall_contact_frames == 0
                    ):
                        entry.route_plan.active_target_index = (
                            entry.route_plan.active_target_index + 1
                        ) % len(entry.route_plan.permanent_waypoints)
                        forced_wp = entry.route_plan.active_waypoint()
                        if forced_wp is not None:
                            entry.route_last_dist = math.hypot(forced_wp.x - state.x, forced_wp.y - state.y)
                        else:
                            entry.route_last_dist = float("inf")
                        entry.route_last_idx = entry.route_plan.active_target_index
                        entry.route_stall_time = 0.0
                        entry.route_idx_stall_time = 0.0
                        entry.route_stall_recover_time = max(entry.route_stall_recover_time, 1.2)
                        entry.waypoint_behind_time = 0.0
                        if decision_logger is not None:
                            decision_logger.log_decision(
                                entry.race_elapsed,
                                entry.instance_name,
                                "braking",
                                "clear_route_stall_watchdog",
                                state.speed,
                                force=True,
                            )

                    if (
                        entry.hard_route_stall_time > 1.8
                        and len(entry.route_plan.permanent_waypoints) > 1
                        and state.state != "crashed"
                    ):
                        # Re-seed the route from the car's current pose so the
                        # active target becomes the first waypoint actually ahead
                        # of the car's nose, instead of only stepping +1 (which
                        # can keep a displaced/spun-out car circling the same
                        # local waypoints instead of driving forward).
                        _seed_route_target_from_pose(
                            entry.route_plan,
                            (state.x, state.y, state.heading_radians),
                            track.start_grid,
                        )
                        forced_wp = entry.route_plan.active_waypoint()
                        if forced_wp is not None:
                            entry.route_last_dist = math.hypot(forced_wp.x - state.x, forced_wp.y - state.y)
                        else:
                            entry.route_last_dist = float("inf")
                        entry.route_last_idx = entry.route_plan.active_target_index
                        entry.route_stall_recover_time = max(entry.route_stall_recover_time, 1.8)
                        entry.hard_route_recenter_time = max(entry.hard_route_recenter_time, 2.0)
                        entry.route_stall_time = 0.0
                        entry.route_idx_stall_time = 0.0
                        entry.waypoint_behind_time = 0.0
                        entry.hard_route_stall_time = 0.0
                        entry.speed_flip_stall_time = 0.0
                        if decision_logger is not None:
                            decision_logger.log_decision(
                                entry.race_elapsed,
                                entry.instance_name,
                                "braking",
                                "hard_route_stall_watchdog",
                                state.speed,
                                force=True,
                            )

                    if entry.hard_route_recenter_time > 0.0:
                        if vision_center_state == "barrier" or state.wall_contact_frames > 0:
                            entry.hard_route_recenter_time = min(2.8, entry.hard_route_recenter_time + dt * 0.5)
                        else:
                            entry.hard_route_recenter_time = max(0.0, entry.hard_route_recenter_time - dt * 1.7)
                else:
                    entry.hard_route_recenter_time = max(0.0, entry.hard_route_recenter_time - dt)

                entry.route_stall_recover_time = max(0.0, entry.route_stall_recover_time - dt)

                if decision_logger is not None:
                    decision_logger.log_tick(
                        entry.race_elapsed,
                        entry.instance_name,
                        state.speed,
                        entry.route_plan.active_target_index,
                        entry.vision_matrix.get("center", "near").state,
                    )

                if state.wall_contact_frames > 0 and prev_wall_contact == 0:
                    entry.barrier_hits += 1
                    if decision_logger is not None:
                        decision_logger.log_decision(
                            entry.race_elapsed,
                            entry.instance_name,
                            "left_track",
                            "car_footprint_off_surface",
                            state.speed,
                            force=True,
                        )

                if state.state == "crashed" and prev_state_name != "crashed" and decision_logger is not None:
                    if state.damage >= 100.0 and state.fuel <= 0.0 and state.tire_health <= 0.0:
                        crash_reason = "damage_fuel_and_tires_depleted"
                    elif state.damage >= 100.0 and state.fuel <= 0.0:
                        crash_reason = "damage_and_fuel_depleted"
                    elif state.tire_health <= 0.0 and state.fuel <= 0.0:
                        crash_reason = "fuel_and_tires_depleted"
                    elif state.damage >= 100.0 and state.tire_health <= 0.0:
                        crash_reason = "damage_and_tires_depleted"
                    elif state.damage >= 100.0:
                        crash_reason = "damage_limit"
                    elif state.fuel <= 0.0:
                        crash_reason = "fuel_depleted"
                    elif state.tire_health <= 0.0:
                        crash_reason = "tire_depleted"
                    else:
                        crash_reason = "unknown"
                    decision_logger.log_decision(
                        entry.race_elapsed,
                        entry.instance_name,
                        "crashed",
                        crash_reason,
                        state.speed,
                        force=True,
                    )

                if state.laps > prev_laps:
                    # Lap-limit completion tracking for series races.
                    if not entry.completed_lap_limit and state.state != "crashed" and state.laps >= series_lap_limit:
                        entry.completed_lap_limit = True
                        entry.finish_time = entry.race_elapsed
                    prev_best_lap = entry.best_lap_seconds
                    lap_time = entry.race_elapsed - entry.lap_start_time
                    # Restore tires to full at each lap completion.
                    state.tire_health = car.starting_tire_health
                    if state.laps % 10 == 0:
                        state.fuel = car.starting_fuel
                    if lap_time > 0.0:
                        entry.last_lap_seconds = lap_time
                    if lap_time > 0.0 and (entry.best_lap_seconds <= 0.0 or lap_time < entry.best_lap_seconds):
                        entry.best_lap_seconds = lap_time
                    entry.lap_start_time = entry.race_elapsed

                    lap_damage = max(0.0, state.damage - entry.last_lap_damage_checkpoint)
                    entry.last_lap_damage_checkpoint = state.damage

                    if lap_time > 0.0 and prev_best_lap > 0.0:
                        if lap_time < prev_best_lap * 0.985:
                            entry.learning.target_speed_bias = min(1.45, entry.learning.target_speed_bias + 0.03)
                            entry.learning.steering_aggression = min(1.35, entry.learning.steering_aggression + 0.015)
                            entry.learning.safety_bias = max(0.72, entry.learning.safety_bias - 0.01)
                        elif lap_time > prev_best_lap * 1.015:
                            entry.learning.target_speed_bias = max(0.72, entry.learning.target_speed_bias - 0.02)
                            entry.learning.safety_bias = min(1.6, entry.learning.safety_bias + 0.02)

                    if lap_damage > 6.0:
                        entry.learning.safety_bias = min(1.6, entry.learning.safety_bias + 0.05)
                        entry.learning.target_speed_bias = max(0.72, entry.learning.target_speed_bias - 0.03)
                        entry.learning.steering_aggression = max(0.75, entry.learning.steering_aggression - 0.02)

                    # Per-lap line preference adaptation. Leader freeze removed,
                    # so all cars continue adapting their lateral line offset.
                    adapt_sign = 1.0 if entry.pass_side_bias >= 0.0 else -1.0
                    delta = 0.0
                    if lap_time > 0.0 and prev_best_lap > 0.0:
                        if lap_time < prev_best_lap * 0.99:
                            delta += car.line_offset_scale * 3.2 * adapt_sign
                        elif lap_time > prev_best_lap * 1.01:
                            delta -= car.line_offset_scale * 2.0 * adapt_sign
                    if lap_damage > 6.0:
                        delta -= car.line_offset_scale * 2.2 * adapt_sign

                    entry.preferred_line_offset += delta
                    entry.preferred_line_offset *= 0.985
                    # Wider line-offset learning range so cars explore more lines.
                    max_offset = max(12.0, car.width * 1.8)
                    entry.preferred_line_offset = max(
                        -max_offset,
                        min(max_offset, entry.preferred_line_offset),
                    )

                # Drift tracking for race stats 2.
                if state.state == "drifting":
                    if entry.current_drift_start is None:
                        entry.current_drift_start = entry.race_elapsed
                else:
                    if entry.current_drift_start is not None:
                        drift_dur = entry.race_elapsed - entry.current_drift_start
                        if drift_dur > entry.max_drift_duration:
                            entry.max_drift_duration = drift_dur
                        entry.current_drift_start = None

                # Crash timing for race stats 2.
                if state.state == "crashed" and entry.first_crash_time is None:
                    entry.first_crash_time = entry.race_elapsed

                entry.race_elapsed += dt
                entry.speed_accum += max(0.0, state.speed)
                entry.speed_samples += 1
                entry.max_race_speed = max(entry.max_race_speed, max(0.0, state.speed))

            _resolve_car_overlaps(sim_cars)

            if all_stopped:
                racing = False
                if infinite_mode:
                    message = "All cars wrecked. Starting next race..."
                else:
                    message = "All cars are crashed/stopped. Press N to restart."
            elif series_active or infinite_mode or (training_active and series_lap_limit > 0):
                unwrecked = [entry for entry in sim_cars if entry.state.state != "crashed"]
                if unwrecked and all(entry.completed_lap_limit for entry in unwrecked):
                    racing = False
                    message = f"Race complete: lap limit of {series_lap_limit} reached."

        if not racing and not race_outcome_saved and sim_cars:
            for entry in sim_cars:
                _finalize_race_outcome(entry)
            if decision_logger is not None:
                decision_logger.write_summary()
                decision_logger.write_race_results(sim_cars)
                decision_logger = None
            race_outcome_saved = True
            if training_active:
                training_completed_races += 1
                _save_car_learning(sim_cars, logs_dir)
            # Award series points if series is active.
            if series_active or infinite_mode:
                series_race_number += 1
                _award_series_points(sim_cars, series_race_number)
                series_completed_races += 1
                if series_active and series_race_target > 0 and series_completed_races >= series_race_target:
                    series_active = False
                    infinite_mode = False
                    # Determine series winner.
                    winner = max(sim_cars, key=lambda e: e.series_stats.points) if sim_cars else None
                    if winner is not None:
                        message = f"Series complete! Winner: {winner.instance_name} ({winner.series_stats.points} pts)"
                    else:
                        message = "Series complete!"

        if training_active and not racing and race_outcome_saved:
            if training_completed_races >= training_total_races:
                training_active = False
                message = f"Training complete: {training_completed_races}/{training_total_races} races."
            else:
                start_race_session(training=True)
        elif infinite_mode and not racing and race_outcome_saved:
            # Infinite loop: hot-reload car configs and start a new race.
            _reload_car_configs(sim_cars, cars_dir)
            start_race_session(training=False)

        if not training_active:
            for idx, entry in enumerate(sim_cars):
                draw_car(screen, entry.state, entry.config, track_transform)
                if entry.state.state == "crashed":
                    if crash_overlay is not None:
                        scale = track_transform[0] if track else 1.0
                        sx, sy = _to_screen((entry.state.x, entry.state.y), track_transform) if track else (int(entry.state.x), int(entry.state.y))
                        overlay_w = max(1, int(entry.config.length * 1.6 * scale))
                        overlay_h = max(1, int(entry.config.width * 2.0 * scale))
                        overlay = pygame.transform.smoothscale(
                            crash_overlay,
                            (overlay_w, overlay_h),
                        )
                        overlay = pygame.transform.rotate(overlay, -math.degrees(entry.state.heading_radians))
                        overlay_rect = overlay.get_rect(center=(sx, sy))
                        screen.blit(overlay, overlay_rect)
                    else:
                        draw_crash_fallback(screen, entry.state, entry.config)

                if selected_car_index is not None and idx == selected_car_index:
                    select_rect = _car_draw_rect(entry.state, entry.config, track_transform).inflate(8, 8)
                    pygame.draw.rect(screen, (255, 220, 120), select_rect, width=2)
                    _draw_selected_car_overlays(screen, entry, track_transform)

        if training_active:
            panel_w = min(780, width - 120)
            panel_h = 180
            panel = pygame.Rect((width - panel_w) // 2, (height - panel_h) // 2, panel_w, panel_h)
            overlay = pygame.Surface((panel.width, panel.height), pygame.SRCALPHA)
            overlay.fill((20, 28, 36, 220))
            screen.blit(overlay, panel.topleft)
            pygame.draw.rect(screen, (96, 128, 156), panel, width=2)

            progress_base = training_completed_races / max(1, training_total_races)
            if racing and training_completed_races < training_total_races:
                progress_base += 0.5 / max(1, training_total_races)
            progress = max(0.0, min(1.0, progress_base))

            all_best_laps = [
                outcome.best_lap_seconds
                for entry in sim_cars
                for outcome in entry.memory.recent_outcomes
                if outcome.best_lap_seconds > 0.0
            ]
            lap_spread_line = "Best Lap Spread: --"
            if len(all_best_laps) >= 2:
                spread = max(all_best_laps) - min(all_best_laps)
                lap_spread_line = f"Best Lap Spread: {spread:.2f}s"

            avg_speed_bias = 0.0
            avg_steer_aggr = 0.0
            if sim_cars:
                avg_speed_bias = sum(entry.learning.target_speed_bias for entry in sim_cars) / len(sim_cars)
                avg_steer_aggr = sum(entry.learning.steering_aggression for entry in sim_cars) / len(sim_cars)

            bar_rect = pygame.Rect(panel.x + 24, panel.y + panel.height - 56, panel.width - 48, 20)
            pygame.draw.rect(screen, (44, 54, 68), bar_rect, border_radius=4)
            fill_w = int(bar_rect.width * progress)
            if fill_w > 0:
                fill_rect = pygame.Rect(bar_rect.x, bar_rect.y, fill_w, bar_rect.height)
                pygame.draw.rect(screen, (88, 188, 130), fill_rect, border_radius=4)
            pygame.draw.rect(screen, (116, 136, 156), bar_rect, width=1, border_radius=4)

            stage_line = (
                f"Running race {training_completed_races + 1}/{training_total_races}"
                if racing
                else f"Completed {training_completed_races}/{training_total_races}"
            )
            overlay_lines = [
                "Training Simulation",
                stage_line,
                f"Speed: {training_speed_multiplier:.0f}x",
                f"Progress: {training_completed_races}/{training_total_races} races",
                lap_spread_line,
                f"Avg Learn: speed_bias={avg_speed_bias:.2f} steer_aggr={avg_steer_aggr:.2f}",
            ]
            draw_lines(screen, font, overlay_lines, panel.x + 24, panel.y + 20, (232, 240, 246))

        if not asr_streaming:
            draw_lines(screen, font, [message], 24, height - 40, (245, 245, 245))

            if load_picker_open:
                picker_title = "Load Car" if load_picker_kind == "car" else "Load Track"
                _, load_picker_rows = draw_file_picker(
                    screen,
                    font,
                    picker_title,
                    [p.name for p in load_picker_files],
                    pygame.mouse.get_pos(),
                )

            header_rects, item_rects = draw_dropdown_menus(
                screen,
                font,
                menus,
                open_menu,
                pygame.mouse.get_pos(),
            )
        if chrome_lb is not None:
            sig = _stream_chrome_signature(sim_cars, series_name, series_completed_races, series_race_target)
            if sig != chrome_sig_prev:
                _render_stream_chrome(
                    chrome_lb,
                    chrome_bottom,
                    sim_cars,
                    font,
                    series_name,
                    series_completed_races,
                    series_race_target,
                    series_logo_scaled,
                )
                chrome_sig_prev = sig
            screen.blit(chrome_lb, leaderboard_pane.topleft)
            screen.blit(chrome_bottom, bottom_pane.topleft)
        pygame.display.flip()
        clock.tick(render_target)

        if streamer is not None:
            # Render the UI at the full window size (so panels/layout fit), then
            # downscale only the captured frame to the capture size before the
            # FIFO. The capture frame must fit the ~1MiB pipe for corruption-free
            # raw RGBA delivery. `screen` is already a Surface, so smoothscale it
            # directly (avoids an extra full-size tobytes/frombuffer each frame -
            # a big win on the CPU-bound renderer).
            if capture_width != width or capture_height != height:
                scaled = pygame.transform.smoothscale(
                    screen, (capture_width, capture_height)
                )
                raw = pygame.image.tobytes(scaled, "RGBA")
            else:
                raw = pygame.image.tobytes(screen, "RGBA")
            streamer.push(raw)

    if streamer is not None:
        streamer.stop()
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
