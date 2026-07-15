from __future__ import annotations

import hashlib
import math
import subprocess
import tempfile
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import pygame

from src.common.config import as_int, read_simple_conf
from src.common.geometry import point_in_polygon
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
    VisionMatrix,
    Waypoint,
)
from src.common.physics import _car_is_on_racing_surface, update_car_state
from src.common.ui import create_default_font, draw_dropdown_menus, draw_file_picker, draw_lines, menu_action_at

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


@dataclass
class StatsDropdownState:
    header_rect: pygame.Rect = field(default_factory=lambda: pygame.Rect(0, 0, 0, 0))
    list_rect: pygame.Rect = field(default_factory=lambda: pygame.Rect(0, 0, 0, 0))
    row_rects: list[tuple[int, pygame.Rect]] = field(default_factory=list)
    open: bool = False
    scroll_index: int = 0


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


def draw_track(surface: pygame.Surface, track) -> None:
    outer_types = [piece.piece_type for piece in track.outer_pieces]
    inner_types = [piece.piece_type for piece in track.inner_pieces]
    outer_path = _smooth_loop(track.outer_points, outer_types)
    inner_path = _smooth_loop(track.inner_points, inner_types)

    pygame.draw.polygon(surface, (10, 10, 10), outer_path)
    pygame.draw.polygon(surface, (42, 145, 75), inner_path)
    pygame.draw.lines(surface, (220, 220, 220), True, outer_path, 3)
    pygame.draw.lines(surface, (220, 220, 220), True, inner_path, 3)
    pygame.draw.rect(surface, (230, 190, 40), track.start_grid)


def draw_car(surface: pygame.Surface, state: CarRuntimeState, car: CarConfig) -> None:
    body = pygame.Surface((int(car.length), int(car.width)), pygame.SRCALPHA)
    body.fill(car.body_color)
    nose = pygame.Rect(int(car.length * 0.7), 0, int(car.length * 0.3), int(car.width))
    pygame.draw.rect(body, car.nose_color, nose)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    rect = rotated.get_rect(center=(state.x, state.y))
    surface.blit(rotated, rect)


def _car_draw_rect(state: CarRuntimeState, car: CarConfig) -> pygame.Rect:
    body = pygame.Surface((int(car.length), int(car.width)), pygame.SRCALPHA)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    return rotated.get_rect(center=(state.x, state.y))


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
    _seed_route_target_from_pose(sim_car.route_plan, sim_car.start_pose, track.start_grid)
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


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _build_centerline(track) -> list[tuple[float, float]]:
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

    target_count = max(10, min(34, n // 11))
    points = _resample_centerline_points(centerline, target_count=target_count, straight_bias=2.1, turn_floor=0.4)
    if len(points) < 4:
        points = centerline

    waypoints = [
        Waypoint(x=pt[0], y=pt[1], kind="permanent", source="generated")
        for pt in points
    ]
    return CarRoutePlan(permanent_waypoints=waypoints)


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


def _draw_selected_car_overlays(screen: pygame.Surface, sim_car: SimCar) -> None:
    state = sim_car.state
    car = sim_car.config
    route = sim_car.route_plan

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

    # Vision cone overlay (70 degrees, 5x length).
    cone_range = car.length * 5.0
    half_angle = math.radians(35.0)
    points = [(state.x, state.y)]
    steps = 12
    for i in range(steps + 1):
        t = i / steps
        angle = state.heading_radians - half_angle + (2.0 * half_angle * t)
        points.append((state.x + math.cos(angle) * cone_range, state.y + math.sin(angle) * cone_range))
    cone = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
    pygame.draw.polygon(cone, (110, 190, 255, 44), points)
    pygame.draw.lines(cone, (130, 220, 255, 140), False, points[1:], width=2)
    screen.blit(cone, (0, 0))

    if route.permanent_waypoints:
        pts = [(wp.x, wp.y) for wp in route.permanent_waypoints]
        if len(pts) > 1:
            pygame.draw.lines(screen, (70, 210, 120), True, pts, width=2)
            preferred_pts = _offset_loop(pts, sim_car.preferred_line_offset)
            if len(preferred_pts) > 1:
                pygame.draw.lines(screen, (255, 150, 70), True, preferred_pts, width=2)
        for idx, wp in enumerate(route.permanent_waypoints):
            color = (255, 230, 130) if idx == route.active_target_index % max(1, len(route.permanent_waypoints)) else (70, 210, 120)
            pygame.draw.circle(screen, color, (int(wp.x), int(wp.y)), 5)

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

    max_range = car.length * 5.0
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
        return point_in_polygon((px, py), track.outer_points) and not point_in_polygon((px, py), track.inner_points)

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

    max_view_range = car.length * 5.0
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
    off_track = not point_in_polygon((state.x, state.y), track.outer_points) or point_in_polygon((state.x, state.y), track.inner_points)
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
        return point_in_polygon((px, py), track.outer_points) and not point_in_polygon((px, py), track.inner_points)

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
            emergency_distance = max(105.0, 74.0 + max(0.0, state.speed) * 0.95 + closing_speed * 2.5)
            if ahead < emergency_distance and closing_speed > 0.5 and not start_line_commit:
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
        if nearest_stopped_ahead < 190.0 and forward_speed > 24.0:
            approach_pressure = max(0.0, 1.0 - nearest_stopped_ahead / 190.0)
            _set_brake(0.35 + approach_pressure * 0.4, "stopped_hazard_approach")
        if nearest_stopped_ahead < 115.0 and forward_speed > 18.0:
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

    if emergency_brake:
        brake = 1.0
        brake_reason = "emergency_hazard"

    return throttle, brake, steering, brake_reason, coast_reason, last_visible_line_point


def _centerline_length(track) -> float:
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
    return point_in_polygon((x, y), track.outer_points) and not point_in_polygon((x, y), track.inner_points)


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


def main() -> int:
    project_dir = Path(__file__).resolve().parents[2]
    conf = read_simple_conf(
        project_dir / "etc" / "tracksim.conf",
        {
            "window_width": "1600",
            "window_height": "900",
            "tracks_dir": "tracks",
            "cars_dir": "cars",
            "default_track": "",
            "training_races": "10",
        },
    )

    width = as_int(conf, "window_width", 1600)
    height = as_int(conf, "window_height", 900)
    training_race_target = max(1, as_int(conf, "training_races", 10))
    tracks_dir = project_dir / conf.get("tracks_dir", "tracks")
    cars_dir = project_dir / conf.get("cars_dir", "cars")
    logs_dir = project_dir / "logs"

    pygame.init()
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("Track Simulation")
    clock = pygame.time.Clock()
    font = create_default_font(22)
    status_font = create_default_font(16)
    crash_overlay = _load_crash_overlay(project_dir)

    track = None
    current_track_path: Path | None = None
    sim_cars: list[SimCar] = []
    selected_car_index: int | None = 0
    racing = False
    race_outcome_saved = True
    decision_logger: RaceDecisionLogger | None = None
    waypoint_density_step = 3
    message = "L load track, C load car, N start/reset race, Del remove selected car, H toggle stats, arrows steer selected car, Q quit"
    open_menu: str | None = None
    item_rects: dict[tuple[str, str], pygame.Rect] = {}
    load_picker_open = False
    load_picker_kind = "track"
    load_picker_files: list[Path] = []
    load_picker_rows: list[tuple[int, pygame.Rect]] = []

    up_pressed = False
    down_pressed = False
    left_pressed = False
    right_pressed = False
    autonomous_enabled = True
    show_car_stats = True
    show_debug_stats = False
    show_race_stats = True
    training_active = False
    training_total_races = training_race_target
    training_completed_races = 0
    training_speed_multiplier = 100.0

    dragging_index: int | None = None
    drag_offset = (0.0, 0.0)
    stats_dropdown = StatsDropdownState()
    active_info_panel: str | None = None
    stats_panel_rect = pygame.Rect(0, 0, 0, 0)
    debug_panel_rect = pygame.Rect(0, 0, 0, 0)
    race_panel_rect = pygame.Rect(0, 0, 0, 0)

    menus = [
        ("Start", ["Load Track", "Load Car", "Remove Selected Car", "Save Track", "Quit"]),
        ("Race", ["Start Race", "Simulate", "Pause/Resume", "Reset Cars", "Increase Waypoint Density", "Decrease Waypoint Density", "Quit Race"]),
        ("Stats", ["Toggle Car Stats", "Toggle Debug Pane", "Toggle Race Stats"]),
    ]

    def any_stats_visible() -> bool:
        return show_car_stats or show_debug_stats or show_race_stats

    def ensure_selected_index() -> None:
        nonlocal selected_car_index
        if not sim_cars:
            selected_car_index = None
            stats_dropdown.open = False
            stats_dropdown.scroll_index = 0
            return
        if selected_car_index is None:
            return
        selected_car_index = max(0, min(selected_car_index, len(sim_cars) - 1))

    def clamp_stats_dropdown_scroll() -> None:
        max_visible_rows = 6
        max_scroll = max(0, len(sim_cars) - max_visible_rows)
        stats_dropdown.scroll_index = max(0, min(stats_dropdown.scroll_index, max_scroll))

    def remove_selected_car() -> None:
        nonlocal selected_car_index, message
        if selected_car_index is None or selected_car_index < 0 or selected_car_index >= len(sim_cars):
            message = "No selected car to remove."
            return
        removed_name = sim_cars[selected_car_index].instance_name
        sim_cars.pop(selected_car_index)
        if not sim_cars:
            selected_car_index = None
            stats_dropdown.open = False
            stats_dropdown.scroll_index = 0
        else:
            selected_car_index = min(selected_car_index, len(sim_cars) - 1)
            clamp_stats_dropdown_scroll()
        message = f"Removed car {removed_name}."

    def add_loaded_car(car_path: Path, instance_name: str | None = None, start_pose: tuple[float, float, float] | None = None) -> None:
        nonlocal selected_car_index
        if track is None:
            return
        loaded = load_car(car_path)
        name = _make_unique_car_name(instance_name or loaded.name, sim_cars)
        profile, learning, pass_side_bias, pace_bias, steer_bias = _build_personality(name, loaded)
        route_plan = _load_route_plan_from_track(track, name)
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
        _seed_route_target_from_pose(sim_cars[inserted_index].route_plan, open_pose, track.start_grid)
        selected_car_index = len(sim_cars) - 1

    def load_track_into_session(chosen: Path, prefix: str = "Loaded track") -> None:
        nonlocal track, current_track_path, sim_cars, selected_car_index, message
        loaded_track = load_track(chosen)
        track = loaded_track
        current_track_path = chosen
        sim_cars = []
        selected_car_index = None

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

        if not sim_cars:
            latest_car = load_latest(cars_dir, ".car")
            if latest_car is not None:
                add_loaded_car(latest_car)

        message = f"{prefix} {chosen.name}."

    def start_race_session(training: bool = False) -> bool:
        nonlocal racing, race_outcome_saved, decision_logger, message
        if track is None:
            message = "Load a track before starting a race."
            return False
        if not sim_cars:
            message = "Load at least one car first."
            return False

        for entry in sim_cars:
            _reset_for_race(entry, track)
        racing = True
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

    running = True
    while running:
        dt = min(clock.get_time() / 1000.0, 0.05)
        if training_active:
            dt *= training_speed_multiplier

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP:
                    up_pressed = True
                elif event.key == pygame.K_DOWN:
                    down_pressed = True
                elif event.key == pygame.K_LEFT:
                    left_pressed = True
                elif event.key == pygame.K_RIGHT:
                    right_pressed = True

                if event.key == pygame.K_q:
                    running = False
                elif event.key == pygame.K_ESCAPE:
                    load_picker_open = False
                    open_menu = None
                    stats_dropdown.open = False
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
                elif event.key == pygame.K_a:
                    autonomous_enabled = not autonomous_enabled
                    message = "Autonomous mode on." if autonomous_enabled else "Manual mode on."
                elif event.key == pygame.K_h:
                    show_car_stats = not show_car_stats
                    message = "Car stats shown." if show_car_stats else "Car stats hidden."
                elif event.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
                    if racing:
                        message = "Pause race before removing cars."
                    else:
                        remove_selected_car()
            elif event.type == pygame.KEYUP:
                if event.key == pygame.K_UP:
                    up_pressed = False
                elif event.key == pygame.K_DOWN:
                    down_pressed = False
                elif event.key == pygame.K_LEFT:
                    left_pressed = False
                elif event.key == pygame.K_RIGHT:
                    right_pressed = False
            elif event.type == pygame.MOUSEWHEEL:
                if stats_dropdown.open and sim_cars:
                    mouse_pos = pygame.mouse.get_pos()
                    if stats_dropdown.list_rect.collidepoint(mouse_pos) or stats_dropdown.header_rect.collidepoint(mouse_pos):
                        stats_dropdown.scroll_index -= event.y
                        clamp_stats_dropdown_scroll()
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
                dragging_index = None
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if any_stats_visible() and sim_cars:
                    if stats_panel_rect.collidepoint(event.pos):
                        active_info_panel = "stats"
                    elif debug_panel_rect.collidepoint(event.pos):
                        active_info_panel = "debug"
                    elif race_panel_rect.collidepoint(event.pos):
                        active_info_panel = "race"
                    else:
                        active_info_panel = None

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

                if stats_dropdown.open:
                    clicked_dropdown = False
                    for idx, rect in stats_dropdown.row_rects:
                        if rect.collidepoint(event.pos):
                            selected_car_index = idx
                            stats_dropdown.open = False
                            clicked_dropdown = True
                            break
                    if clicked_dropdown:
                        continue
                    if not stats_dropdown.header_rect.collidepoint(event.pos):
                        stats_dropdown.open = False

                if stats_dropdown.header_rect.collidepoint(event.pos) and show_car_stats and sim_cars:
                    stats_dropdown.open = not stats_dropdown.open
                    if stats_dropdown.open:
                        clamp_stats_dropdown_scroll()
                    open_menu = None
                    continue

                if track is not None and sim_cars:
                    clicked_car = False
                    for idx in range(len(sim_cars) - 1, -1, -1):
                        entry = sim_cars[idx]
                        car_rect = _car_draw_rect(entry.state, entry.config)
                        if car_rect.collidepoint(event.pos):
                            selected_car_index = idx
                            if not racing:
                                dragging_index = idx
                                drag_offset = (entry.state.x - event.pos[0], entry.state.y - event.pos[1])
                            open_menu = None
                            clicked_car = True
                            break
                    if dragging_index is not None:
                        continue
                    if not clicked_car:
                        if not racing:
                            selected_car_index = None

                action = menu_action_at(event.pos, header_rects, item_rects)
                if action is None:
                    open_menu = None
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
                    elif action.menu == "Start" and action.item == "Save Track":
                        if track is None:
                            message = "Load a track before saving."
                        else:
                            if not isinstance(track.metadata, dict):
                                track.metadata = {}
                            track.metadata["car_starts"] = _serialize_car_starts(sim_cars)
                            track.metadata["car_routes"] = _serialize_car_routes(sim_cars)
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
                    elif action.menu == "Race" and action.item == "Pause/Resume":
                        if training_active:
                            message = "Pause/Resume disabled during simulation."
                            continue
                        if not sim_cars:
                            message = "Load at least one car first."
                        else:
                            racing = not racing
                            message = "Race resumed." if racing else "Race paused."
                    elif action.menu == "Race" and action.item == "Reset Cars":
                        if not sim_cars:
                            message = "No cars loaded."
                        else:
                            for entry in sim_cars:
                                _reset_for_race(entry, track)
                            racing = False
                            race_outcome_saved = True
                            message = "All cars reset to saved starting positions."
                    elif action.menu == "Race" and action.item == "Increase Waypoint Density":
                        if track is None or not sim_cars:
                            message = "Load a track and at least one car first."
                        elif racing:
                            message = "Pause race before changing waypoint density."
                        else:
                            for entry in sim_cars:
                                _increase_permanent_waypoints(track, entry.route_plan, increment=waypoint_density_step)
                                _seed_route_target_from_pose(entry.route_plan, entry.start_pose, track.start_grid)
                            message = f"Increased waypoint density by {waypoint_density_step}."
                    elif action.menu == "Race" and action.item == "Decrease Waypoint Density":
                        if track is None or not sim_cars:
                            message = "Load a track and at least one car first."
                        elif racing:
                            message = "Pause race before changing waypoint density."
                        else:
                            for entry in sim_cars:
                                _decrease_permanent_waypoints(track, entry.route_plan, decrement=waypoint_density_step)
                                _seed_route_target_from_pose(entry.route_plan, entry.start_pose, track.start_grid)
                            message = f"Decreased waypoint density by {waypoint_density_step}."
                    elif action.menu == "Race" and action.item == "Quit Race":
                        racing = False
                        race_outcome_saved = False
                        training_active = False
                        training_completed_races = 0
                        message = "Race ended."
                    elif action.menu == "Stats" and action.item == "Toggle Car Stats":
                        show_car_stats = not show_car_stats
                        if not show_car_stats:
                            stats_dropdown.open = False
                        message = "Car stats shown." if show_car_stats else "Car stats hidden."
                    elif action.menu == "Stats" and action.item == "Toggle Debug Pane":
                        show_debug_stats = not show_debug_stats
                        message = "Debug pane shown." if show_debug_stats else "Debug pane hidden."
                    elif action.menu == "Stats" and action.item == "Toggle Race Stats":
                        show_race_stats = not show_race_stats
                        message = "Race stats shown." if show_race_stats else "Race stats hidden."

        ensure_selected_index()
        clamp_stats_dropdown_scroll()

        if training_active:
            screen.fill((18, 24, 32))
        else:
            screen.fill((42, 145, 75))
            if track is not None:
                draw_track(screen, track)

        if racing and track is not None and sim_cars:
            all_stopped = True
            current_leader = max(
                sim_cars,
                key=lambda item: (item.state.laps, item.state.distance_traveled),
            ) if sim_cars else None
            for idx, entry in enumerate(sim_cars):
                state = entry.state
                car = entry.config
                entry.line_offset_frozen = (
                    current_leader is not None and current_leader.instance_name == entry.instance_name
                )
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

                if autonomous_enabled:
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
                else:
                    throttle = 0.0
                    brake = 0.0
                    steering = 0.0

                if selected_car_index is not None and idx == selected_car_index and (left_pressed or right_pressed or up_pressed or down_pressed):
                    throttle = 1.0 if up_pressed else 0.0
                    brake = 1.0 if down_pressed else 0.0
                    steering = 0.0
                    if left_pressed:
                        steering -= 1.0
                    if right_pressed:
                        steering += 1.0

                prev_laps = state.laps
                prev_wall_contact = state.wall_contact_frames
                prev_state_name = state.state

                update_car_state(state, car, track, dt, throttle, brake, steering)
                in_start_zone = pygame.Rect(track.start_grid).collidepoint(state.x, state.y)
                lap0_wrap_allowed = in_start_zone and state.left_start_zone
                allow_route_wrap = state.laps > 0 or lap0_wrap_allowed
                update_lap_counter(state, track)
                # Require full footprint on racing surface before advancing route index.
                can_advance_waypoint = _car_is_on_racing_surface(state, car, track) and state.wall_contact_frames == 0
                reached_waypoint = False
                if can_advance_waypoint:
                    reached_waypoint = entry.route_plan.advance_if_reached(
                        state.x,
                        state.y,
                        threshold=max(40.0, car.length * 0.9),
                        allow_wrap=allow_route_wrap,
                    )
                if reached_waypoint:
                    entry.post_waypoint_boost_time = 1.3
                else:
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
                        entry.route_plan.active_target_index = (
                            entry.route_plan.active_target_index + 1
                        ) % len(entry.route_plan.permanent_waypoints)
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

                    # Per-lap line preference adaptation for non-leaders.
                    # Leader line is frozen to stabilize pace while leading.
                    if not entry.line_offset_frozen:
                        adapt_sign = 1.0 if entry.pass_side_bias >= 0.0 else -1.0
                        delta = 0.0
                        if lap_time > 0.0 and prev_best_lap > 0.0:
                            if lap_time < prev_best_lap * 0.99:
                                delta += car.line_offset_scale * 1.8 * adapt_sign
                            elif lap_time > prev_best_lap * 1.01:
                                delta -= car.line_offset_scale * 1.2 * adapt_sign
                        if lap_damage > 6.0:
                            delta -= car.line_offset_scale * 1.4 * adapt_sign

                        entry.preferred_line_offset += delta
                        entry.preferred_line_offset *= 0.98
                        max_offset = max(6.0, car.width * 0.95)
                        entry.preferred_line_offset = max(
                            -max_offset,
                            min(max_offset, entry.preferred_line_offset),
                        )

                entry.race_elapsed += dt
                entry.speed_accum += max(0.0, state.speed)
                entry.speed_samples += 1
                entry.max_race_speed = max(entry.max_race_speed, max(0.0, state.speed))

            _resolve_car_overlaps(sim_cars)

            if all_stopped:
                racing = False
                message = "All cars are crashed/stopped. Press N to restart."

        if not racing and not race_outcome_saved and sim_cars:
            for entry in sim_cars:
                _finalize_race_outcome(entry)
            if decision_logger is not None:
                decision_logger.write_summary()
                decision_logger = None
            race_outcome_saved = True
            if training_active:
                training_completed_races += 1

        if training_active and not racing and race_outcome_saved:
            if training_completed_races >= training_total_races:
                training_active = False
                message = f"Training complete: {training_completed_races}/{training_total_races} races."
            else:
                start_race_session(training=True)

        if not training_active:
            for idx, entry in enumerate(sim_cars):
                draw_car(screen, entry.state, entry.config)
                if entry.state.state == "crashed":
                    if crash_overlay is not None:
                        overlay = pygame.transform.smoothscale(
                            crash_overlay,
                            (int(entry.config.length * 1.6), int(entry.config.width * 2.0)),
                        )
                        overlay = pygame.transform.rotate(overlay, -math.degrees(entry.state.heading_radians))
                        overlay_rect = overlay.get_rect(center=(entry.state.x, entry.state.y))
                        screen.blit(overlay, overlay_rect)
                    else:
                        draw_crash_fallback(screen, entry.state, entry.config)

                if selected_car_index is not None and idx == selected_car_index:
                    select_rect = _car_draw_rect(entry.state, entry.config).inflate(8, 8)
                    pygame.draw.rect(screen, (255, 220, 120), select_rect, width=2)
                    _draw_selected_car_overlays(screen, entry)

        if not training_active and any_stats_visible() and sim_cars:
            stats_index = selected_car_index if selected_car_index is not None else 0
            selected = sim_cars[stats_index]
            base_panel_rect = pygame.Rect(width - 296, height - 212, 284, 200)

            if show_car_stats:
                panel_rect = base_panel_rect
                stats_panel_rect = panel_rect.copy()
                panel = pygame.Surface((panel_rect.width, panel_rect.height), pygame.SRCALPHA)
                panel.fill((24, 28, 32, 212 if active_info_panel == "stats" else 132))
                screen.blit(panel, panel_rect.topleft)
                pygame.draw.rect(screen, (96, 116, 138) if active_info_panel == "stats" else (54, 66, 82), panel_rect, width=1)

                stats_dropdown.header_rect = pygame.Rect(panel_rect.x + 12, panel_rect.y + 10, panel_rect.w - 24, 26)
                pygame.draw.rect(screen, (54, 62, 74), stats_dropdown.header_rect, border_radius=3)
                draw_lines(screen, status_font, [f"Car: {selected.instance_name}"], stats_dropdown.header_rect.x + 8, stats_dropdown.header_rect.y + 4, (235, 235, 235))

                stats_dropdown.row_rects = []
                stats_dropdown.list_rect = pygame.Rect(0, 0, 0, 0)
                if stats_dropdown.open:
                    row_h = 24
                    max_visible_rows = 6
                    max_scroll = max(0, len(sim_cars) - max_visible_rows)
                    stats_dropdown.scroll_index = max(0, min(stats_dropdown.scroll_index, max_scroll))

                    visible_start = stats_dropdown.scroll_index
                    visible_end = min(len(sim_cars), visible_start + max_visible_rows)
                    visible_count = max(0, visible_end - visible_start)
                    list_h = visible_count * row_h
                    # Render the list above the header so it does not overlap panel stats text.
                    list_y = stats_dropdown.header_rect.y - list_h - 2
                    if list_y < 6:
                        list_y = stats_dropdown.header_rect.bottom + 2

                    if visible_count > 0:
                        stats_dropdown.list_rect = pygame.Rect(
                            stats_dropdown.header_rect.x,
                            list_y,
                            stats_dropdown.header_rect.w,
                            list_h,
                        )
                        pygame.draw.rect(screen, (16, 20, 26), stats_dropdown.list_rect, border_radius=3)
                        pygame.draw.rect(screen, (78, 90, 106), stats_dropdown.list_rect, width=1, border_radius=3)

                    y = list_y
                    for idx in range(visible_start, visible_end):
                        entry = sim_cars[idx]
                        row = pygame.Rect(stats_dropdown.header_rect.x, y, stats_dropdown.header_rect.w, row_h)
                        hovered = row.collidepoint(pygame.mouse.get_pos())
                        pygame.draw.rect(screen, (70, 84, 102) if hovered else (30, 36, 44), row, border_radius=2)
                        draw_lines(screen, status_font, [entry.instance_name], row.x + 6, row.y + 2, (235, 235, 235))
                        stats_dropdown.row_rects.append((idx, row))
                        y += row_h

                compact_lines = [
                    f"State: {selected.state.state}",
                    f"Speed: {selected.state.speed:.1f}",
                    f"Fuel: {selected.state.fuel:.1f}",
                    f"Tire: {selected.state.tire_health:.1f}",
                    f"Damage: {selected.state.damage:.1f}",
                    f"Laps: {selected.state.laps}",
                    f"Best Lap: {selected.best_lap_seconds:.2f}s" if selected.best_lap_seconds > 0 else "Best Lap: --",
                    f"Memories: {len(selected.memory.recent_outcomes)}/10",
                ]
                draw_lines(screen, status_font, compact_lines, panel_rect.x + 12, panel_rect.y + 44, (235, 235, 235))
            else:
                stats_panel_rect = pygame.Rect(0, 0, 0, 0)
                stats_dropdown.open = False

            if show_debug_stats:
                debug_rect = pygame.Rect(base_panel_rect.x - 272, base_panel_rect.y, 260, 200)
                debug_panel_rect = debug_rect.copy()
                debug_panel = pygame.Surface((debug_rect.width, debug_rect.height), pygame.SRCALPHA)
                debug_panel.fill((20, 24, 28, 212 if active_info_panel == "debug" else 132))
                screen.blit(debug_panel, debug_rect.topleft)

                pygame.draw.rect(screen, (96, 116, 138) if active_info_panel == "debug" else (72, 86, 102), debug_rect, width=1)
                draw_lines(screen, status_font, ["Debug"], debug_rect.x + 10, debug_rect.y + 10, (200, 225, 255))

                matrix_rows = [
                    " ".join(selected.vision_matrix.get(xb, yb).state[:2] for xb in VISION_X_BINS)
                    for yb in VISION_Y_BINS
                ]
                last_lap_line = f"Last Lap: {selected.last_lap_seconds:.2f}s" if selected.last_lap_seconds > 0 else "Last Lap: --"
                dbg_lines = [
                    last_lap_line,
                    f"Lap Start: {selected.lap_start_time:.2f}s",
                    f"Race T: {selected.race_elapsed:.2f}s",
                    f"Dist: {selected.state.distance_traveled:.1f}",
                    f"line offset: {selected.preferred_line_offset:.1f}",
                    f"line frozen: {'yes' if selected.line_offset_frozen else 'no'}",
                    f"Hits: {selected.barrier_hits}",
                    f"v: ({selected.state.vx:.1f}, {selected.state.vy:.1f})",
                    f"yaw_rate: {selected.state.yaw_rate:.2f}",
                    f"route idx: {selected.route_plan.active_target_index}",
                    f"vision n/m/f: {matrix_rows[0]} | {matrix_rows[1]} | {matrix_rows[2]}",
                ]
                draw_lines(screen, status_font, dbg_lines, debug_rect.x + 10, debug_rect.y + 34, (225, 225, 225))
            else:
                debug_panel_rect = pygame.Rect(0, 0, 0, 0)

            if show_race_stats:
                race_w = 284
                race_h = 160
                pane_gap = 12

                # Keep race stats aligned with the bottom panel row, preferring to sit to the left.
                anchor_rect = debug_panel_rect if show_debug_stats else stats_panel_rect
                if anchor_rect.width <= 0:
                    anchor_rect = base_panel_rect

                race_x = anchor_rect.x - race_w - pane_gap
                race_y = anchor_rect.y + max(0, anchor_rect.height - race_h)

                race_x = max(8, min(race_x, width - race_w - 8))
                race_y = max(8, min(race_y, height - race_h - 8))
                race_rect = pygame.Rect(race_x, race_y, race_w, race_h)
                race_panel_rect = race_rect.copy()
                race_panel = pygame.Surface((race_rect.width, race_rect.height), pygame.SRCALPHA)
                race_panel.fill((22, 30, 34, 212 if active_info_panel == "race" else 132))
                screen.blit(race_panel, race_rect.topleft)
                pygame.draw.rect(screen, (94, 122, 130) if active_info_panel == "race" else (66, 86, 92), race_rect, width=1)

                best, worst = _best_worst_lap(sim_cars)
                leader_entry = max(
                    sim_cars,
                    key=lambda entry: (entry.state.laps, entry.state.distance_traveled),
                ) if sim_cars else None
                top_speed_entry = max(sim_cars, key=lambda entry: entry.max_race_speed) if sim_cars else None
                leader_laps = max((entry.state.laps for entry in sim_cars), default=0)
                leader_line = f"Race Leader: {leader_entry.instance_name}" if leader_entry is not None else "Race Leader: --"
                top_speed_line = (
                    f"Top Speed: {top_speed_entry.instance_name} {top_speed_entry.max_race_speed:.1f}"
                    if top_speed_entry is not None
                    else "Top Speed: --"
                )
                best_line = f"Best: {best[0]} {best[1]:.2f}s" if best else "Best: --"
                worst_line = f"Worst: {worst[0]} {worst[1]:.2f}s" if worst else "Worst: --"
                race_lines = [
                    "Race Stats",
                    leader_line,
                    top_speed_line,
                    f"Laps Completed: {leader_laps}",
                    best_line,
                    worst_line,
                ]
                draw_lines(screen, status_font, race_lines, race_rect.x + 10, race_rect.y + 10, (230, 242, 234))
            else:
                race_panel_rect = pygame.Rect(0, 0, 0, 0)
        else:
            stats_panel_rect = pygame.Rect(0, 0, 0, 0)
            debug_panel_rect = pygame.Rect(0, 0, 0, 0)
            race_panel_rect = pygame.Rect(0, 0, 0, 0)
            active_info_panel = None

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

        mode_line = "AUTO (A)" if autonomous_enabled else "MANUAL (A)"
        mode_color = (180, 210, 255) if autonomous_enabled else (240, 210, 170)
        draw_lines(screen, font, [mode_line], width - 178, 46, mode_color)

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
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
