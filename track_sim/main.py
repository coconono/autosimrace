#!/usr/bin/env python3
"""Minimal pygame entry point for track_sim."""

from __future__ import annotations

import sys

import pygame

WINDOW_WIDTH = 960
WINDOW_HEIGHT = 540
WINDOW_TITLE = "track_sim"
FPS = 60
BG_COLOR = (18, 24, 38)
TRACK_COLOR = (230, 230, 230)


def draw_placeholder_track(surface: pygame.Surface) -> None:
    """Draw a simple placeholder shape so the window is not empty."""
    points = [
        (180, 120),
        (760, 120),
        (830, 220),
        (760, 420),
        (180, 420),
        (110, 220),
    ]
    pygame.draw.lines(surface, TRACK_COLOR, True, points, width=10)


def main() -> int:
    pygame.init()
    clock = pygame.time.Clock()
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    pygame.display.set_caption(WINDOW_TITLE)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        screen.fill(BG_COLOR)
        draw_placeholder_track(screen)
        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
