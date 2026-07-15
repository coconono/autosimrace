from __future__ import annotations

import math

from .geometry import is_on_racing_surface
from .models import CarConfig, CarRuntimeState, TrackLayout


def _blend_heading(a: float, b: float, weight_b: float) -> float:
    weight_b = max(0.0, min(1.0, weight_b))
    weight_a = 1.0 - weight_b
    x = weight_a * math.cos(a) + weight_b * math.cos(b)
    y = weight_a * math.sin(a) + weight_b * math.sin(b)
    return math.atan2(y, x)


def _centerline_points(track: TrackLayout) -> list[tuple[float, float]]:
    count = min(len(track.outer_points), len(track.inner_points))
    return [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(count)
    ]


def _closest_index(point: tuple[float, float], points: list[tuple[float, float]]) -> int:
    best_i = 0
    best_d = float("inf")
    for i, p in enumerate(points):
        dx = p[0] - point[0]
        dy = p[1] - point[1]
        d = dx * dx + dy * dy
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _car_surface_samples(state: CarRuntimeState, car: CarConfig, scale: float = 0.95) -> list[tuple[float, float]]:
    half_len = car.length * 0.5 * scale
    half_wid = car.width * 0.5 * scale
    c = math.cos(state.heading_radians)
    s = math.sin(state.heading_radians)

    local_points = [
        (0.0, 0.0),
        (half_len, 0.0),
        (-half_len, 0.0),
        (0.0, half_wid),
        (0.0, -half_wid),
        (half_len, half_wid),
        (half_len, -half_wid),
        (-half_len, half_wid),
        (-half_len, -half_wid),
    ]

    out: list[tuple[float, float]] = []
    for lx, ly in local_points:
        wx = state.x + lx * c - ly * s
        wy = state.y + lx * s + ly * c
        out.append((wx, wy))
    return out


def _car_is_on_racing_surface(state: CarRuntimeState, car: CarConfig, track: TrackLayout) -> bool:
    return all(is_on_racing_surface(sample, track) for sample in _car_surface_samples(state, car))


def update_car_state(
    state: CarRuntimeState,
    car: CarConfig,
    track: TrackLayout,
    dt: float,
    throttle: float,
    brake: float,
    steering: float,
) -> None:
    if state.state == "crashed":
        return

    accel = throttle * (car.max_speed * 1.8) - brake * (car.max_speed * 1.45)
    state.speed += accel * dt
    state.speed *= 0.994
    state.speed = max(-car.max_speed * 0.3, min(state.speed, car.max_speed))

    grip = max(0.12, state.tire_health / 100.0)

    # Yaw inertia: steering requests a target yaw-rate that the body approaches over time.
    speed_ratio = min(1.0, abs(state.speed) / max(car.max_speed, 1.0))
    drift_intent = (
        abs(steering) > 0.4
        and speed_ratio > 0.16
    )
    target_yaw_rate = steering * (2.8 + speed_ratio * 2.6) * max(0.55, speed_ratio)
    if drift_intent:
        target_yaw_rate *= 1.18
    yaw_response = min(1.0, dt * (4.8 + grip * 2.2))
    if drift_intent:
        yaw_response *= 0.9
    state.yaw_rate += (target_yaw_rate - state.yaw_rate) * yaw_response
    state.yaw_rate *= max(0.0, 1.0 - dt * 0.6)
    state.heading_radians += state.yaw_rate * dt

    # Velocity inertia: world velocity lags behind desired heading/speed.
    desired_vx = math.cos(state.heading_radians) * state.speed
    desired_vy = math.sin(state.heading_radians) * state.speed
    align_gain = min(1.0, dt * (3.6 + grip * 4.2))
    if drift_intent:
        # Keep some world-velocity inertia while rotating so the rear can step out.
        align_gain *= 0.72
    state.vx += (desired_vx - state.vx) * align_gain
    state.vy += (desired_vy - state.vy) * align_gain

    # Lateral slip damping keeps cars stable while still preserving momentum.
    forward_x = math.cos(state.heading_radians)
    forward_y = math.sin(state.heading_radians)
    lateral_x = -forward_y
    lateral_y = forward_x
    forward_vel = state.vx * forward_x + state.vy * forward_y
    lateral_vel = state.vx * lateral_x + state.vy * lateral_y
    lateral_damp = max(0.0, 1.0 - dt * (2.0 + grip * 3.0))
    if drift_intent:
        lateral_damp *= 0.62
    lateral_vel *= lateral_damp
    state.vx = forward_x * forward_vel + lateral_x * lateral_vel
    state.vy = forward_y * forward_vel + lateral_y * lateral_vel
    state.speed = forward_vel
    state.speed = max(-car.max_speed * 0.3, min(state.speed, car.max_speed))

    if abs(state.speed) < 2 and throttle <= 0.01 and brake <= 0.01:
        state.state = "stopped"
    elif throttle <= 0.05 and brake <= 0.05 and abs(steering) <= 0.2 and state.speed > 2.0:
        state.state = "coasting"
    elif brake > 0.1:
        state.state = "braking"
    elif steering < -0.2:
        state.state = "turning_left"
    elif steering > 0.2:
        state.state = "turning_right"
    elif state.speed < 0:
        state.state = "reversing"
    else:
        state.state = "moving_forward"

    drift_trigger = drift_intent
    if drift_trigger:
        state.state = "drifting"

    prev_x = state.x
    prev_y = state.y
    state.last_x = prev_x
    state.last_y = prev_y
    state.x += state.vx * dt
    state.y += state.vy * dt

    if not _car_is_on_racing_surface(state, car, track):
        # Stay where physics put the car; do not snap or teleport back to the track.
        # Off-track penalties are applied over time until the car returns to the surface.
        state.wall_contact_frames += 1

        # Loose surface: lower effective traction and bleed momentum gradually.
        state.vx *= max(0.0, 1.0 - dt * 1.8)
        state.vy *= max(0.0, 1.0 - dt * 1.8)
        state.yaw_rate *= max(0.0, 1.0 - dt * 2.2)

        # Off-track no longer causes damage; keep elevated tire wear.
        offtrack_tire_loss_per_sec = 22.0
        speed_wear_factor = min(2.5, abs(state.speed) / max(car.max_speed * 0.35, 1.0))
        state.tire_health = max(0.0, state.tire_health - (offtrack_tire_loss_per_sec + 10.0 * speed_wear_factor) * dt)

        # Active recovery: bias motion back toward centerline to avoid prolonged wall grinding.
        centerline = _centerline_points(track)
        if centerline:
            idx = _closest_index((state.x, state.y), centerline)
            cx, cy = centerline[idx]
            rx = cx - state.x
            ry = cy - state.y
            rlen = math.hypot(rx, ry)
            if rlen > 1e-6:
                ux = rx / rlen
                uy = ry / rlen
                recover_push = min(90.0, 36.0 + rlen * 0.22)
                state.vx += ux * recover_push * dt
                state.vy += uy * recover_push * dt
                recover_heading = math.atan2(uy, ux)
                state.heading_radians = _blend_heading(state.heading_radians, recover_heading, min(0.85, dt * 3.0))

        forward_x = math.cos(state.heading_radians)
        forward_y = math.sin(state.heading_radians)
        state.speed = state.vx * forward_x + state.vy * forward_y
    else:
        # Decay contact memory instead of hard reset to avoid one-frame oscillation
        # between recovery and coasting near barrier edges.
        state.wall_contact_frames = max(0, state.wall_contact_frames - 1)

    moved = math.hypot(state.x - prev_x, state.y - prev_y)
    state.distance_traveled += moved

    state.tire_health = max(0.0, state.tire_health - (abs(state.speed) / max(car.max_speed, 1.0)) * dt * 0.15)
    state.fuel = max(0.0, state.fuel - (0.15 + abs(state.speed) * 0.0014) * dt)

    if state.damage >= 100.0 or state.fuel <= 0.0 or state.tire_health <= 0.0:
        state.state = "crashed"
        state.speed = 0.0
        state.vx = 0.0
        state.vy = 0.0
        state.yaw_rate = 0.0
