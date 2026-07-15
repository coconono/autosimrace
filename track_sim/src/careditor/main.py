from __future__ import annotations

from pathlib import Path

import pygame

from src.common.config import as_int, read_simple_conf
from src.common.io import load_car, save_car
from src.common.models import CarConfig
from src.common.ui import create_default_font, draw_dropdown_menus, draw_file_picker, draw_lines, menu_action_at


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def load_first_car(cars_dir: Path) -> CarConfig:
    candidates = sorted(cars_dir.glob("*.car"))
    if not candidates:
        return CarConfig()
    return load_car(candidates[0])


def load_car_sprite(project_dir: Path) -> pygame.Surface | None:
    sprite_path = project_dir / "images" / "car.drawio.png"
    if not sprite_path.exists():
        return None
    try:
        return pygame.image.load(str(sprite_path)).convert_alpha()
    except pygame.error:
        return None


def _get_color_component(car: CarConfig, key: str) -> int:
    if key == "body_r":
        return int(car.body_color[0])
    if key == "body_g":
        return int(car.body_color[1])
    if key == "body_b":
        return int(car.body_color[2])
    if key == "nose_r":
        return int(car.nose_color[0])
    if key == "nose_g":
        return int(car.nose_color[1])
    return int(car.nose_color[2])


def _set_color_component(car: CarConfig, key: str, value: int) -> None:
    value = int(clamp(value, 0, 255))
    if key == "body_r":
        car.body_color = (value, int(car.body_color[1]), int(car.body_color[2]))
    elif key == "body_g":
        car.body_color = (int(car.body_color[0]), value, int(car.body_color[2]))
    elif key == "body_b":
        car.body_color = (int(car.body_color[0]), int(car.body_color[1]), value)
    elif key == "nose_r":
        car.nose_color = (value, int(car.nose_color[1]), int(car.nose_color[2]))
    elif key == "nose_g":
        car.nose_color = (int(car.nose_color[0]), value, int(car.nose_color[2]))
    elif key == "nose_b":
        car.nose_color = (int(car.nose_color[0]), int(car.nose_color[1]), value)


def draw_car_preview(surface: pygame.Surface, font, car: CarConfig, sprite: pygame.Surface | None) -> None:
    panel = pygame.Rect(surface.get_width() - 520, 70, 470, 320)
    pygame.draw.rect(surface, (32, 38, 48), panel)
    pygame.draw.rect(surface, (90, 102, 124), panel, width=2)

    title = font.render("Car Preview", True, (236, 236, 236))
    surface.blit(title, (panel.x + 14, panel.y + 10))

    preview_area = panel.inflate(-40, -80)
    preview_area.y += 24

    scale = 2.4
    target_w = max(12, min(int(car.length * scale), preview_area.w - 20))
    target_h = max(8, min(int(car.width * scale), preview_area.h - 20))

    rect = pygame.Rect(0, 0, target_w, target_h)
    rect.center = preview_area.center
    pygame.draw.rect(surface, car.body_color, rect)
    nose = rect.copy()
    nose.width = max(4, rect.width // 4)
    nose.left = rect.right - nose.width
    pygame.draw.rect(surface, car.nose_color, nose)

    body_swatch = pygame.Rect(panel.x + 14, panel.bottom - 60, 26, 16)
    nose_swatch = pygame.Rect(panel.x + 130, panel.bottom - 60, 26, 16)
    pygame.draw.rect(surface, car.body_color, body_swatch)
    pygame.draw.rect(surface, car.nose_color, nose_swatch)
    pygame.draw.rect(surface, (200, 200, 200), body_swatch, width=1)
    pygame.draw.rect(surface, (200, 200, 200), nose_swatch, width=1)
    surface.blit(font.render("Body", True, (176, 190, 210)), (body_swatch.right + 8, body_swatch.y - 2))
    surface.blit(font.render("Nose", True, (176, 190, 210)), (nose_swatch.right + 8, nose_swatch.y - 2))

    if sprite is not None:
        label = font.render("Reference image: images/car.drawio.png", True, (176, 190, 210))
        surface.blit(label, (panel.x + 14, panel.bottom - 34))


def main() -> int:
    project_dir = Path(__file__).resolve().parents[2]
    conf = read_simple_conf(
        project_dir / "etc" / "careditor.conf",
        {
            "window_width": "1600",
            "window_height": "900",
            "cars_dir": "cars",
        },
    )

    width = as_int(conf, "window_width", 1600)
    height = as_int(conf, "window_height", 900)
    cars_dir = project_dir / conf.get("cars_dir", "cars")
    cars_dir.mkdir(parents=True, exist_ok=True)

    pygame.init()
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("Car Editor")
    clock = pygame.time.Clock()
    font = create_default_font(24)
    car_sprite = load_car_sprite(project_dir)

    car = load_first_car(cars_dir)
    message = "Use up/down to select, left/right to edit, N rename, S save, L load."
    edit_name = False
    selected = 0
    open_menu: str | None = None
    header_rects: dict[str, pygame.Rect] = {}
    item_rects: dict[tuple[str, str], pygame.Rect] = {}
    load_picker_open = False
    load_picker_files: list[Path] = []
    load_picker_panel = pygame.Rect(0, 0, 0, 0)
    load_picker_rows: list[tuple[int, pygame.Rect]] = []

    menus = [
        ("Start", ["Save", "Load", "Quit"]),
        ("Car Name", ["Edit"]),
    ]

    fields = [
        ("Car Name", "name", 0.0),
        ("Length", "length", 1.0),
        ("Width", "width", 1.0),
        ("Mass", "mass", 10.0),
        ("Max Speed", "max_speed", 5.0),
        ("Line Offset Scale", "line_offset_scale", 0.05),
        ("Starting Tire", "starting_tire_health", 1.0),
        ("Starting Fuel", "starting_fuel", 1.0),
        ("Body R", "body_r", 5.0),
        ("Body G", "body_g", 5.0),
        ("Body B", "body_b", 5.0),
        ("Nose R", "nose_r", 5.0),
        ("Nose G", "nose_g", 5.0),
        ("Nose B", "nose_b", 5.0),
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
                        car = load_car(chosen)
                        message = f"Loaded {chosen.name}."
                    load_picker_open = False
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
                    elif action.menu == "Start" and action.item == "Save":
                        if not car.name.strip():
                            message = "Car name cannot be empty."
                        else:
                            path = cars_dir / f"{car.name.strip().replace(' ', '_')}.car"
                            save_car(path, car)
                            message = f"Saved {path.name}."
                    elif action.menu == "Start" and action.item == "Load":
                        load_picker_files = sorted(cars_dir.glob("*.car"))
                        load_picker_open = True
                        message = "Select a car file to load."
                    elif action.menu == "Car Name" and action.item == "Edit":
                        selected = 0
                        edit_name = True
                        message = "Name edit mode. Type and press Enter."
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    edit_name = False
                    open_menu = None
                    load_picker_open = False
                elif event.key == pygame.K_q:
                    running = False
                elif edit_name:
                    if event.key == pygame.K_RETURN:
                        edit_name = False
                        message = f"Name set to {car.name}."
                    elif event.key == pygame.K_BACKSPACE:
                        car.name = car.name[:-1]
                    elif event.unicode.isprintable() and len(car.name) < 32:
                        car.name += event.unicode
                else:
                    if event.key == pygame.K_UP:
                        selected = (selected - 1) % len(fields)
                    elif event.key == pygame.K_DOWN:
                        selected = (selected + 1) % len(fields)
                    elif event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                        _, key, step = fields[selected]
                        sign = 1 if event.key == pygame.K_RIGHT else -1
                        if key == "name":
                            edit_name = True
                            message = "Name edit mode. Type and press Enter."
                        elif key.startswith("body_") or key.startswith("nose_"):
                            value = _get_color_component(car, key) + int(sign * step)
                            _set_color_component(car, key, value)
                        else:
                            value = float(getattr(car, key)) + sign * step
                            if key == "length":
                                value = clamp(value, 20.0, 140.0)
                            elif key == "width":
                                value = clamp(value, 10.0, 80.0)
                            elif key == "mass":
                                value = clamp(value, 200.0, 4000.0)
                            elif key == "max_speed":
                                value = clamp(value, 40.0, 700.0)
                            elif key == "line_offset_scale":
                                value = clamp(value, 0.0, 1.5)
                            elif key in ("starting_tire_health", "starting_fuel"):
                                value = clamp(value, 1.0, 100.0)
                            setattr(car, key, value)
                    elif event.key == pygame.K_n:
                        selected = 0
                        edit_name = True
                        message = "Name edit mode. Type and press Enter."
                    elif event.key == pygame.K_s:
                        if not car.name.strip():
                            message = "Car name cannot be empty."
                        else:
                            path = cars_dir / f"{car.name.strip().replace(' ', '_')}.car"
                            save_car(path, car)
                            message = f"Saved {path.name}."
                    elif event.key == pygame.K_l:
                        load_picker_files = sorted(cars_dir.glob("*.car"))
                        load_picker_open = True
                        message = "Select a car file to load."

        screen.fill((20, 25, 30))

        lines = ["Car Editor", ""]
        for idx, (label, key, _) in enumerate(fields):
            if key.startswith("body_") or key.startswith("nose_"):
                value = _get_color_component(car, key)
            else:
                value = getattr(car, key)
            if isinstance(value, float):
                rendered = f"{label}: {value:.1f}"
            else:
                rendered = f"{label}: {value}"
            prefix = "> " if idx == selected else "  "
            lines.append(prefix + rendered)

        lines += ["", message]
        if edit_name:
            lines.append("Editing name: Enter to accept, Esc to cancel")

        draw_lines(screen, font, lines, 60, 70, (236, 236, 236))
        draw_car_preview(screen, font, car, car_sprite)

        if load_picker_open:
            load_picker_panel, load_picker_rows = draw_file_picker(
                screen,
                font,
                "Load Car",
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
