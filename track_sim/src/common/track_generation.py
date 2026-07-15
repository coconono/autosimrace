from __future__ import annotations

import math
import random
import time
from typing import Any

from .geometry import point_in_polygon
from .models import BarrierPiece, TrackLayout


OUTER_TYPES = ["short_straight", "long_straight", "curve"]
INNER_TYPES = ["short_barrier", "long_barrier", "curve"]


def _build_piece(piece_prefix: str, index: int, piece_type: str, position: tuple[float, float], orientation: int, count: int) -> BarrierPiece:
    left = f"{piece_prefix}_{(index - 1) % count}"
    right = f"{piece_prefix}_{(index + 1) % count}"
    return BarrierPiece(
        piece_id=f"{piece_prefix}_{index}",
        piece_type=piece_type,
        position=position,
        orientation=orientation,
        connects_to=(left, right),
    )


def _toward_center(point: tuple[float, float], center: tuple[float, float], amount: float) -> tuple[float, float]:
    x, y = point
    cx, cy = center
    dx = cx - x
    dy = cy - y
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return point
    return (x + dx / length * amount, y + dy / length * amount)


def _line_intersection(
    p1: tuple[float, float],
    d1: tuple[float, float],
    p2: tuple[float, float],
    d2: tuple[float, float],
) -> tuple[float, float] | None:
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < 1e-6:
        return None
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    t = (dx * d2[1] - dy * d2[0]) / cross
    return (p1[0] + d1[0] * t, p1[1] + d1[1] * t)


def _inset_polygon(points: list[tuple[float, float]], inset: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points

    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)

    def inward_normal(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return (0.0, 0.0)
        tx = dx / length
        ty = dy / length
        n1 = (-ty, tx)
        n2 = (ty, -tx)
        mx = (a[0] + b[0]) * 0.5
        my = (a[1] + b[1]) * 0.5
        to_center = (cx - mx, cy - my)
        dot1 = n1[0] * to_center[0] + n1[1] * to_center[1]
        dot2 = n2[0] * to_center[0] + n2[1] * to_center[1]
        return n1 if dot1 >= dot2 else n2

    out: list[tuple[float, float]] = []

    for i in range(len(points)):
        prev_pt = points[(i - 1) % len(points)]
        curr_pt = points[i]
        next_pt = points[(i + 1) % len(points)]

        in_dx = curr_pt[0] - prev_pt[0]
        in_dy = curr_pt[1] - prev_pt[1]
        out_dx = next_pt[0] - curr_pt[0]
        out_dy = next_pt[1] - curr_pt[1]

        in_len = math.hypot(in_dx, in_dy)
        out_len = math.hypot(out_dx, out_dy)
        if in_len < 1e-6 or out_len < 1e-6:
            out.append(curr_pt)
            continue

        in_dir = (in_dx / in_len, in_dy / in_len)
        out_dir = (out_dx / out_len, out_dy / out_len)

        in_norm = inward_normal(prev_pt, curr_pt)
        out_norm = inward_normal(curr_pt, next_pt)

        line1_point = (curr_pt[0] + in_norm[0] * inset, curr_pt[1] + in_norm[1] * inset)
        line2_point = (curr_pt[0] + out_norm[0] * inset, curr_pt[1] + out_norm[1] * inset)
        intersect = _line_intersection(line1_point, in_dir, line2_point, out_dir)
        if intersect is None:
            avg_norm = ((in_norm[0] + out_norm[0]) * 0.5, (in_norm[1] + out_norm[1]) * 0.5)
            norm_len = math.hypot(avg_norm[0], avg_norm[1])
            if norm_len < 1e-6:
                out.append(line1_point)
            else:
                out.append((curr_pt[0] + avg_norm[0] / norm_len * inset, curr_pt[1] + avg_norm[1] / norm_len * inset))
        else:
            out.append(intersect)

    return out


def _piece_type_for_point(points: list[tuple[float, float]], index: int) -> str:
    prev_point = points[(index - 1) % len(points)]
    curr_point = points[index]
    next_point = points[(index + 1) % len(points)]

    incoming = (curr_point[0] - prev_point[0], curr_point[1] - prev_point[1])
    outgoing = (next_point[0] - curr_point[0], next_point[1] - curr_point[1])
    cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
    if cross != 0:
        return "curve"

    seg_len = abs(outgoing[0]) + abs(outgoing[1])
    return "long_straight" if seg_len >= 260 else "short_straight"


def _is_on_surface(point: tuple[float, float], outer_points: list[tuple[float, float]], inner_points: list[tuple[float, float]]) -> bool:
    return point_in_polygon(point, outer_points) and not point_in_polygon(point, inner_points)


def _grid_is_valid(
    grid: tuple[float, float, float, float],
    outer_points: list[tuple[float, float]],
    inner_points: list[tuple[float, float]],
) -> bool:
    x, y, w, h = grid
    inset = max(0.5, min(w, h) * 0.1)
    test_points = [
        (x + inset, y + inset),
        (x + w - inset, y + inset),
        (x + inset, y + h - inset),
        (x + w - inset, y + h - inset),
        (x + w * 0.5, y + h * 0.5),
    ]
    return all(_is_on_surface(point, outer_points, inner_points) for point in test_points)


def _choose_start_grid(
    outer_points: list[tuple[float, float]],
    inner_points: list[tuple[float, float]],
    inner_pieces: list[BarrierPiece],
    lane_width: float,
) -> tuple[tuple[float, float, float, float], int]:
    # Requirement preference: adjacent to a short inner barrier.
    preferred = [i for i, piece in enumerate(inner_pieces) if piece.piece_type == "short_barrier"]
    fallback = [i for i, piece in enumerate(inner_pieces) if piece.piece_type != "curve"]
    candidate_indices = preferred or fallback or [0]

    candidate_indices = sorted(
        candidate_indices,
        key=lambda i: max(
            abs(outer_points[i][0] - inner_points[i][0]),
            abs(outer_points[i][1] - inner_points[i][1]),
        )
        / (min(abs(outer_points[i][0] - inner_points[i][0]), abs(outer_points[i][1] - inner_points[i][1])) + 1e-6),
        reverse=True,
    )

    bridge_thicknesses = [
        max(10.0, lane_width * 0.24),
        max(8.0, lane_width * 0.2),
        max(6.0, lane_width * 0.16),
    ]

    for index in candidate_indices:
        ox, oy = outer_points[index]
        ix, iy = inner_points[index]

        for thickness in bridge_thicknesses:
            if abs(ox - ix) >= abs(oy - iy):
                x = min(ix, ox)
                w = abs(ox - ix)
                y = (iy + oy) * 0.5 - thickness * 0.5
                h = thickness
            else:
                y = min(iy, oy)
                h = abs(oy - iy)
                x = (ix + ox) * 0.5 - thickness * 0.5
                w = thickness

            grid = (x, y, max(1.0, w), max(1.0, h))
            if _grid_is_valid(grid, outer_points, inner_points):
                return grid, index

    # Last resort: lane-spanning bridge at the first barrier pair.
    ox, oy = outer_points[0]
    ix, iy = inner_points[0]
    thickness = max(8.0, lane_width * 0.2)
    if abs(ox - ix) >= abs(oy - iy):
        x = min(ix, ox)
        w = abs(ox - ix)
        y = (iy + oy) * 0.5 - thickness * 0.5
        h = thickness
    else:
        y = min(iy, oy)
        h = abs(oy - iy)
        x = (ix + ox) * 0.5 - thickness * 0.5
        w = thickness
    return (x, y, max(1.0, w), max(1.0, h)), 0


def generate_track(seed: int | None = None, width: int = 1600, height: int = 900, lane_width: float = 70.0) -> TrackLayout:
    rng = random.Random(seed)

    margin_x = rng.randint(170, 250)
    margin_y = rng.randint(130, 190)
    right = width - margin_x
    bottom = height - margin_y
    left = margin_x
    top = margin_y

    # Eight-piece rectangular loop with variable midpoint splits.
    top_mid = (left + right) / 2 + rng.randint(-80, 80)
    bottom_mid = (left + right) / 2 + rng.randint(-80, 80)
    left_mid = (top + bottom) / 2 + rng.randint(-60, 60)
    right_mid = (top + bottom) / 2 + rng.randint(-60, 60)

    outer_points = [
        (left, top),
        (top_mid, top),
        (right, top),
        (right, right_mid),
        (right, bottom),
        (bottom_mid, bottom),
        (left, bottom),
        (left, left_mid),
    ]

    inner_points = _inset_polygon(outer_points, lane_width)

    outer_pieces: list[BarrierPiece] = []
    inner_pieces: list[BarrierPiece] = []
    for index, point in enumerate(outer_points):
        next_point = outer_points[(index + 1) % len(outer_points)]
        direction = (next_point[0] - point[0], next_point[1] - point[1])
        if abs(direction[0]) > abs(direction[1]):
            orientation = 0 if direction[0] >= 0 else 180
        else:
            orientation = 90 if direction[1] >= 0 else 270

        outer_type = _piece_type_for_point(outer_points, index)
        inner_type = "curve" if outer_type == "curve" else ("long_barrier" if outer_type == "long_straight" else "short_barrier")

        outer_pieces.append(_build_piece("outer", index, outer_type, point, orientation, len(outer_points)))
        inner_pieces.append(_build_piece("inner", index, inner_type, inner_points[index], orientation, len(inner_points)))

    if not any(piece.piece_type == "short_barrier" for piece in inner_pieces):
        for idx, piece in enumerate(inner_pieces):
            if piece.piece_type != "curve":
                inner_pieces[idx].piece_type = "short_barrier"
                outer_pieces[idx].piece_type = "short_straight"
                break

    start_grid, start_grid_index = _choose_start_grid(outer_points, inner_points, inner_pieces, lane_width)

    metadata: dict[str, Any] = {
        "seed": seed,
        "created_unix": int(time.time()),
        "lane_width": lane_width,
        "curve_count": sum(1 for piece in outer_pieces if piece.piece_type == "curve"),
        "start_grid_index": start_grid_index,
    }

    return TrackLayout(
        name=f"track_{int(time.time())}",
        outer_pieces=outer_pieces,
        inner_pieces=inner_pieces,
        outer_points=outer_points,
        inner_points=inner_points,
        start_grid=start_grid,
        metadata=metadata,
    )
