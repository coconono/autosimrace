from __future__ import annotations

import random
from pathlib import Path

import pygame

from src.common.config import as_int, read_simple_conf
from src.common.geometry import is_on_racing_surface, validate_track
from src.common.io import load_track, save_track
from src.common.track_generation import generate_track
from src.common.ui import create_default_font, draw_dropdown_menus, draw_file_picker, draw_lines, menu_action_at


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
    pygame.draw.rect(surface, (220, 190, 20), track.start_grid, width=0)


def load_latest_track(tracks_dir: Path):
    candidates = sorted(tracks_dir.glob("*.track"))
    if not candidates:
        return None
    return load_track(candidates[-1])


def generate_valid_track(width: int, height: int, lane_width: float):
    for _ in range(200):
        candidate = generate_track(seed=random.randint(1, 999999), width=width, height=height, lane_width=lane_width)
        if validate_track(candidate):
            return candidate
    return None


def _grid_samples(grid: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    gx, gy, gw, gh = grid
    inset_x = max(1.0, min(3.0, gw * 0.08))
    inset_y = max(1.0, min(3.0, gh * 0.08))
    return [
        (gx + inset_x, gy + inset_y),
        (gx + gw - inset_x, gy + inset_y),
        (gx + inset_x, gy + gh - inset_y),
        (gx + gw - inset_x, gy + gh - inset_y),
        (gx + gw * 0.5, gy + gh * 0.5),
    ]


def _grid_on_surface(track, grid: tuple[float, float, float, float]) -> bool:
    return all(is_on_racing_surface(sample, track) for sample in _grid_samples(grid))


def main() -> int:
    project_dir = Path(__file__).resolve().parents[2]
    conf = read_simple_conf(
        project_dir / "etc" / "trackgen.conf",
        {
            "window_width": "1600",
            "window_height": "900",
            "tracks_dir": "tracks",
            "lane_width": "90",
        },
    )

    width = as_int(conf, "window_width", 1600)
    height = as_int(conf, "window_height", 900)
    lane_width = float(as_int(conf, "lane_width", 90))
    tracks_dir = project_dir / conf.get("tracks_dir", "tracks")
    tracks_dir.mkdir(parents=True, exist_ok=True)

    pygame.init()
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("Track Generator")
    clock = pygame.time.Clock()
    font = create_default_font(22)

    track = generate_valid_track(width, height, lane_width)
    message = "G generate, R reset, N rename, S save, L load latest, D discard, Q quit"
    name_edit = False
    name_buffer = track.name if track else "new_track"
    open_menu: str | None = None
    header_rects: dict[str, pygame.Rect] = {}
    item_rects: dict[tuple[str, str], pygame.Rect] = {}
    load_picker_open = False
    load_picker_files: list[Path] = []
    load_picker_rows: list[tuple[int, pygame.Rect]] = []
    dragging_start_grid = False
    start_grid_drag_offset = (0.0, 0.0)

    menus = [
        ("Start", ["Load", "Save", "Quit"]),
        ("Generate", ["Generate", "Reset"]),
        ("Validate", ["Name", "Save", "Quit", "Discard"]),
    ]

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if load_picker_open:
                    chosen_index = None
                    for idx, rect in load_picker_rows:
                        if rect.collidepoint(event.pos):
                            chosen_index = idx
                            break
                    if chosen_index is not None and chosen_index < len(load_picker_files):
                        chosen = load_picker_files[chosen_index]
                        track = load_track(chosen)
                        name_buffer = track.name
                        message = f"Loaded {chosen.name}."
                    load_picker_open = False
                    continue

                if track is not None:
                    start_grid_rect = pygame.Rect(track.start_grid)
                    if start_grid_rect.collidepoint(event.pos):
                        dragging_start_grid = True
                        start_grid_drag_offset = (start_grid_rect.x - event.pos[0], start_grid_rect.y - event.pos[1])
                        message = "Dragging start grid."
                        continue

                action = menu_action_at(event.pos, header_rects, item_rects)
                if action is None:
                    open_menu = None
                elif action.item == "":
                    open_menu = None if open_menu == action.menu else action.menu
                else:
                    open_menu = None
                    if action.menu == "Start" and action.item == "Quit":
                        running = False
                    elif action.menu == "Start" and action.item == "Load":
                        load_picker_files = sorted(tracks_dir.glob("*.track"))
                        load_picker_open = True
                        message = "Select a track file to load."
                    elif action.menu == "Start" and action.item == "Save":
                        if track is None:
                            message = "Nothing to save. Generate or load a track first."
                        else:
                            safe_name = track.name.strip().replace(" ", "_")
                            path = tracks_dir / f"{safe_name}.track"
                            save_track(path, track)
                            message = f"Saved {path.name}."
                    elif action.menu == "Generate" and action.item == "Generate":
                        track = generate_valid_track(width, height, lane_width)
                        if track is None:
                            message = "Failed to generate a valid track after many attempts."
                        else:
                            name_buffer = track.name
                            message = f"Generated {track.name}."
                    elif action.menu == "Generate" and action.item == "Reset":
                        track = None
                        message = "Track reset."
                    elif action.menu == "Validate" and action.item == "Discard":
                        track = generate_valid_track(width, height, lane_width)
                        if track:
                            name_buffer = track.name
                            message = "Discarded old track and generated a new one."
                    elif action.menu == "Validate" and action.item == "Name":
                        if track is not None:
                            name_buffer = track.name
                            name_edit = True
                            message = "Rename mode. Type and press Enter."
                    elif action.menu == "Validate" and action.item == "Save":
                        if track is None:
                            message = "Nothing to save. Generate or load a track first."
                        else:
                            safe_name = track.name.strip().replace(" ", "_")
                            path = tracks_dir / f"{safe_name}.track"
                            save_track(path, track)
                            message = f"Saved {path.name}."
                    elif action.menu == "Validate" and action.item == "Quit":
                        running = False
            elif event.type == pygame.MOUSEMOTION:
                if dragging_start_grid and track is not None:
                    gx, gy, gw, gh = track.start_grid
                    new_x = float(event.pos[0] + start_grid_drag_offset[0])
                    new_y = float(event.pos[1] + start_grid_drag_offset[1])
                    candidate = (new_x, new_y, gw, gh)
                    if _grid_on_surface(track, candidate):
                        track.start_grid = candidate
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if dragging_start_grid:
                    dragging_start_grid = False
                    if track is not None:
                        message = "Start grid moved."
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    load_picker_open = False
                    open_menu = None
                if name_edit:
                    if event.key == pygame.K_RETURN:
                        if track:
                            track.name = name_buffer.strip() or track.name
                        name_edit = False
                        message = f"Name set to {name_buffer.strip() or 'unchanged'}."
                    elif event.key == pygame.K_ESCAPE:
                        name_edit = False
                    elif event.key == pygame.K_BACKSPACE:
                        name_buffer = name_buffer[:-1]
                    elif event.unicode.isprintable() and len(name_buffer) < 40:
                        name_buffer += event.unicode
                    continue

                if event.key == pygame.K_q:
                    running = False
                elif event.key == pygame.K_g:
                    track = generate_valid_track(width, height, lane_width)
                    if track is None:
                        message = "Failed to generate a valid track after many attempts."
                    else:
                        name_buffer = track.name
                        message = f"Generated {track.name}."
                elif event.key == pygame.K_r:
                    track = None
                    message = "Track reset."
                elif event.key == pygame.K_d:
                    track = generate_valid_track(width, height, lane_width)
                    if track:
                        name_buffer = track.name
                        message = "Discarded old track and generated a new one."
                elif event.key == pygame.K_n:
                    if track is not None:
                        name_buffer = track.name
                        name_edit = True
                        message = "Rename mode. Type and press Enter."
                elif event.key == pygame.K_s:
                    if track is None:
                        message = "Nothing to save. Generate or load a track first."
                    else:
                        safe_name = track.name.strip().replace(" ", "_")
                        path = tracks_dir / f"{safe_name}.track"
                        save_track(path, track)
                        message = f"Saved {path.name}."
                elif event.key == pygame.K_l:
                    load_picker_files = sorted(tracks_dir.glob("*.track"))
                    load_picker_open = True
                    message = "Select a track file to load."

        screen.fill((42, 145, 75))

        if track is not None:
            draw_track(screen, track)
            info = [
                f"Track: {track.name}",
                f"Outer pieces: {len(track.outer_pieces)}",
                f"Inner pieces: {len(track.inner_pieces)}",
                "Drag yellow start grid to reposition on track",
            ]
            draw_lines(screen, font, info, 24, 56, (245, 245, 245))
        else:
            draw_lines(screen, font, ["No track loaded."], 24, 56, (245, 245, 245))

        draw_lines(screen, font, [message], 24, height - 40, (245, 245, 245))
        if name_edit:
            draw_lines(screen, font, [f"Name: {name_buffer}"], 24, height - 72, (245, 210, 90))

        if load_picker_open:
            _, load_picker_rows = draw_file_picker(
                screen,
                font,
                "Load Track",
                [p.name for p in load_picker_files],
                pygame.mouse.get_pos(),
            )

        header_rects, item_rects = draw_dropdown_menus(
            screen,
            font,
            [
                ("Start", ["Load", "Save", "Quit"]),
                ("Generate", ["Generate", "Reset"]),
                ("Validate", ["Name", "Save", "Quit", "Discard"]),
            ],
            open_menu,
            pygame.mouse.get_pos(),
        )

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
