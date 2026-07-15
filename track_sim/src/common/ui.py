from __future__ import annotations

import sys
from dataclasses import dataclass

import pygame


MENU_BG = (30, 35, 45)
MENU_TEXT = (240, 240, 240)
MENU_ITEM_BG = (45, 52, 64)
MENU_ITEM_HOVER = (60, 70, 86)
PICKER_BG = (24, 28, 34)
PICKER_BORDER = (95, 106, 126)


@dataclass(frozen=True)
class MenuAction:
    menu: str
    item: str


class _FreeTypeFontAdapter:
    def __init__(self, font_obj):
        self._font = font_obj

    def render(self, text: str, _antialias: bool, color: tuple[int, int, int]) -> pygame.Surface:
        surface, _ = self._font.render(text, color)
        return surface


def create_default_font(size: int):
    """Create a font object with a robust fallback for environments where pygame.font fails."""
    try:
        if not pygame.get_init():
            pygame.init()
        if sys.version_info >= (3, 14):
            raise RuntimeError("Use _freetype fallback on Python 3.14+")
        if pygame.font.get_init() is False:
            pygame.font.init()
        return pygame.font.SysFont("monospace", size)
    except Exception:
        # pygame.freetype imports pygame.sysfont, which can recurse into
        # pygame.font on some interpreter/platform combos (for example Python 3.14).
        # Use the low-level extension directly to avoid that path.
        import pygame._freetype as ft

        if not pygame.get_init():
            pygame.init()
        ft.init()
        return _FreeTypeFontAdapter(ft.Font(None, size))


def draw_menu_bar(surface: pygame.Surface, font: pygame.font.Font, labels: list[str]) -> None:
    pygame.draw.rect(surface, MENU_BG, (0, 0, surface.get_width(), 34))
    x = 12
    for label in labels:
        text = font.render(label, True, MENU_TEXT)
        surface.blit(text, (x, 8))
        x += text.get_width() + 28


def draw_dropdown_menus(
    surface: pygame.Surface,
    font,
    menus: list[tuple[str, list[str]]],
    open_menu: str | None,
    mouse_pos: tuple[int, int] | None = None,
) -> tuple[dict[str, pygame.Rect], dict[tuple[str, str], pygame.Rect]]:
    """Render top-level menus and (optionally) an open dropdown list."""
    pygame.draw.rect(surface, MENU_BG, (0, 0, surface.get_width(), 34))
    header_rects: dict[str, pygame.Rect] = {}
    item_rects: dict[tuple[str, str], pygame.Rect] = {}

    x = 8
    for menu_name, _ in menus:
        label_surface = font.render(menu_name, True, MENU_TEXT)
        width = label_surface.get_width() + 18
        rect = pygame.Rect(x, 2, width, 30)
        if open_menu == menu_name:
            pygame.draw.rect(surface, MENU_ITEM_BG, rect, border_radius=4)
        surface.blit(label_surface, (rect.x + 9, rect.y + 8))
        header_rects[menu_name] = rect
        x += width + 6

    if open_menu is None:
        return header_rects, item_rects

    for menu_name, items in menus:
        if menu_name != open_menu:
            continue
        header = header_rects[menu_name]
        y = header.bottom + 2
        width = max(header.width, 160)
        for item in items:
            item_rect = pygame.Rect(header.x, y, width, 28)
            hovered = mouse_pos is not None and item_rect.collidepoint(mouse_pos)
            pygame.draw.rect(surface, MENU_ITEM_HOVER if hovered else MENU_ITEM_BG, item_rect)
            text_surface = font.render(item, True, MENU_TEXT)
            surface.blit(text_surface, (item_rect.x + 8, item_rect.y + 6))
            item_rects[(menu_name, item)] = item_rect
            y += 28

    return header_rects, item_rects


def menu_action_at(
    position: tuple[int, int],
    header_rects: dict[str, pygame.Rect],
    item_rects: dict[tuple[str, str], pygame.Rect],
) -> MenuAction | None:
    for menu_name, rect in header_rects.items():
        if rect.collidepoint(position):
            return MenuAction(menu=menu_name, item="")
    for (menu_name, item), rect in item_rects.items():
        if rect.collidepoint(position):
            return MenuAction(menu=menu_name, item=item)
    return None


def draw_file_picker(
    surface: pygame.Surface,
    font,
    title: str,
    entries: list[str],
    mouse_pos: tuple[int, int] | None = None,
) -> tuple[pygame.Rect, list[tuple[int, pygame.Rect]]]:
    """Draw a centered picker overlay with clickable file entries."""
    max_rows = min(max(len(entries), 1), 12)
    panel_w = min(900, surface.get_width() - 80)
    panel_h = 56 + max_rows * 30 + 16
    panel = pygame.Rect((surface.get_width() - panel_w) // 2, (surface.get_height() - panel_h) // 2, panel_w, panel_h)

    pygame.draw.rect(surface, PICKER_BG, panel)
    pygame.draw.rect(surface, PICKER_BORDER, panel, width=2)

    title_surface = font.render(title, True, MENU_TEXT)
    surface.blit(title_surface, (panel.x + 12, panel.y + 10))

    item_rects: list[tuple[int, pygame.Rect]] = []
    y = panel.y + 42
    shown = entries[:12]
    for idx, item in enumerate(shown):
        row = pygame.Rect(panel.x + 10, y, panel.w - 20, 26)
        hovered = mouse_pos is not None and row.collidepoint(mouse_pos)
        pygame.draw.rect(surface, MENU_ITEM_HOVER if hovered else MENU_ITEM_BG, row, border_radius=3)
        text_surface = font.render(item, True, MENU_TEXT)
        surface.blit(text_surface, (row.x + 8, row.y + 4))
        item_rects.append((idx, row))
        y += 30

    if not entries:
        empty_surface = font.render("No files found.", True, MENU_TEXT)
        surface.blit(empty_surface, (panel.x + 12, panel.y + 46))

    return panel, item_rects


def draw_lines(surface: pygame.Surface, font: pygame.font.Font, lines: list[str], x: int, y: int, color: tuple[int, int, int]) -> None:
    line_y = y
    for line in lines:
        text = font.render(line, True, color)
        surface.blit(text, (x, line_y))
        line_y += 24
