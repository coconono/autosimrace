from __future__ import annotations

from .models import BarrierPiece, TrackLayout


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = (yi > y) != (yj > y)
        if intersects:
            denom = (yj - yi)
            if denom == 0:
                j = i
                continue
            x_cross = (xj - xi) * (y - yi) / denom + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def validate_piece_connections(pieces: list[BarrierPiece]) -> bool:
    piece_ids = {piece.piece_id for piece in pieces}
    if len(piece_ids) != len(pieces):
        return False
    for piece in pieces:
        if len(piece.connects_to) != 2:
            return False
        left, right = piece.connects_to
        if left == right or left not in piece_ids or right not in piece_ids:
            return False
    return True


def validate_track(track: TrackLayout) -> bool:
    if len(track.outer_pieces) != len(track.inner_pieces):
        return False
    if len(track.outer_points) < 4 or len(track.inner_points) < 4:
        return False
    if not validate_piece_connections(track.outer_pieces):
        return False
    if not validate_piece_connections(track.inner_pieces):
        return False

    # Reject obvious overlaps by ensuring barrier piece centers are unique.
    outer_positions = {piece.position for piece in track.outer_pieces}
    inner_positions = {piece.position for piece in track.inner_pieces}
    if len(outer_positions) != len(track.outer_pieces):
        return False
    if len(inner_positions) != len(track.inner_pieces):
        return False

    return True


def is_on_racing_surface(point: tuple[float, float], track: TrackLayout) -> bool:
    in_outer = point_in_polygon(point, track.outer_points)
    in_inner = point_in_polygon(point, track.inner_points)
    return in_outer and not in_inner
