# Specsheet: Loading Screens

## Overview

Add a full-screen loading overlay that appears before each race in session mode (qualifying and main series). The overlay displays race context information (race type and number) on a green background with bold black text, holds for a minimum duration, and covers all other UI elements during the countdown.

## Objective

Deliver a loading/splash screen that:

1. Displays before each qualifying race in session mode.
2. Displays before each main series race in session mode.
3. Shows for a minimum of 3 seconds (auto-dismiss after the duration).
4. Covers all other UI elements while visible.
5. Shows the race type ("Qualifying" or "Main Series") and race number.
6. Uses a green background with large, bold, black text.

## Source Prompt

`track_sim/prompts/08-loadingscreens.prompt.md`

## Prompt Review Findings

### Conflicts / Ambiguities in original prompt

1. **Green color code unspecified.** The prompt says "green background" but doesn't specify which shade. There is already a grass green `(42, 145, 75)` used for the track pane in the codebase. This is a natural candidate for consistency — the loading screen will match the track pane green rather than introducing a new color.

2. **"Large bold black text" sizing.** The prompt says "large bold black text" but doesn't specify a font size. The existing `render_text_fit` / `create_default_font` utilities support variable sizes. The text should be large enough to be clearly legible at the configured window size (1280x720) — a font size of ~48px centered on screen is appropriate.

3. **Minimum 3 seconds vs. user dismissal.** The prompt says "minimum of 3 seconds" but doesn't specify whether the user can click/dismiss early. Resolution: the loading screen auto-disappears after 3 seconds; any mouse click or keypress also dismisses it early after the minimum has elapsed. This prevents blocking the UI while the 3-second minimum ensures readability.

4. **Scope: session mode only, or all race starts?** The prompt says "display before each qualifying race" and "display before each main series race". This implies session mode only. Single "Start Race" and infinite mode races do not get loading screens. The loading screen is a session-mode feature.

5. **Which race number to display?** During qualifying, the loading screen should show which qualifying race within the total (e.g., "Qualifying 2/6") and the car type. During the main series, it should show the main series race number (e.g., "Main Series Race 1"). This mirrors the session context line format already used in the series stats pane.

6. **Fade transition unspecified.** The prompt says "have a fade away transition to the race view" but doesn't specify duration, easing, or whether the fade applies to the loading screen itself or crossfades into the race view. Resolution: the loading screen will alpha-fade out over ~0.5 seconds before the race view appears. During the fade, the loading screen remains on top with decreasing opacity, revealing the fully rendered race view underneath. No crossfade animation is needed on the race view side — the loading surface simply alpha-blends out.

7. **Interaction with accelerated simulation / training mode.** Loading screens should NOT appear during Simulate (training) mode — they are a visual/interactive feature only. The loading overlay only renders when `not training_active`.

### Resolution

1. Use the existing grass green `(42, 145, 75)` as the background color.
2. Use bold black text at a ~48px font size centered on screen.
3. Minimum 3-second hold; after 3 seconds, mouse click or any keypress dismisses early.
4. Loading screens appear only in session mode, before each qualifying and main series race.
5. Format: `"Qualifying N/M — Type: <type_name>"` during qualifying; `"Main Series Race N"` during main series.
6. Fade-out transition: after the 3-second minimum (or early dismiss), the loading screen alpha-fades out over 0.5 seconds before the race starts. Both the green background and text fade together.
7. Skip loading screens during `training_active` (simulate mode).

## Scope

### In scope

1. Loading screen overlay rendering function (green background, bold black centered text).
2. Timer-based auto-dismissal (3 seconds minimum).
3. Input-based early dismissal (click or keypress after minimum duration).
4. Fade-out transition animation (alpha fade over ~0.5 seconds after dismissal).
5. Integration with session mode: trigger before each qualifying race and each main series race.
6. Display of race type + race number + car type (during qualifying).
7. Drawing the loading screen on top of all other UI elements (including the track pane).
8. Blocking physics simulation while the loading screen is active.
9. Blocking event handling (menu clicks, car selection, keyboard shortcuts) while active.

### Out of scope

1. Loading screens for non-session modes (single "Start Race", infinite mode, standard series mode).
2. Loading screens during Simulate/training mode.
3. Progress bars, spinners, or complex animated elements beyond the simple fade-out transition.

### Dismissal Logic

In the main loop event processing:

```python
if loading_screen_active and (event.type == pygame.MOUSEBUTTONDOWN or event.type == pygame.KEYDOWN):
    if time.time() - loading_screen_start_time >= MIN_LOADING_SECONDS:
        loading_screen_active = False
```

In the main loop after rendering:

```python
if pending_race_start and not loading_screen_active:
    pending_race_start = False
    start_race_session(training=False)
```

Auto-dismiss occurs when `time.time() - loading_screen_start_time >= MIN_LOADING_SECONDS` is detected at the start of a new frame (checked before physics updates).

### Event Blocking

During `loading_screen_active`, the event loop should `continue` past all normal handling:

```python
if loading_screen_active:
    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN):
        if time.time() - loading_screen_start_time >= MIN_LOADING_SECONDS:
            loading_screen_active = False
    continue  # skip all other event processing
```

This prevents menu actions, car selection, dragging, and keyboard shortcuts from leaking through the overlay.

### Rendering Priority

The loading screen is rendered at the very end, after all other UI elements, so it appears on top:

```python
if loading_screen_active:
    _draw_loading_screen(screen, loading_screen_text, width, height)
    pygame.display.flip()
    clock.tick(render_target)
    continue  # go to next frame (skip physics updates too)
```

### Fade-Out Transition

After dismissal (either auto-dismiss or early input), the loading screen does not disappear instantly. Instead, it enters a **fade-out phase**:

1. A new state variable `loading_screen_fading: bool = True` is set when `loading_screen_active` transitions to `False`.
2. During the fade-out phase, the main loop continues to render normally (race view underneath), then the loading screen overlay is blended on top at decreasing alpha.
3. The fade lasts `FADE_DURATION = 0.5` seconds (wall clock).
4. Each frame, alpha is computed as `max(0, 255 * (1 - elapsed / FADE_DURATION))`.
5. The loading surface (green + text) is rendered to a temporary surface with per-pixel alpha, then `set_alpha()` is applied before blitting onto the screen.
6. When alpha reaches 0 (or `elapsed >= FADE_DURATION`), `loading_screen_fading` is set to `False` and `pending_race_start` triggers `start_race_session()`.

```python
if loading_screen_fading:
    elapsed = time.time() - loading_screen_fade_start
    alpha = max(0, 255 * (1.0 - elapsed / FADE_DURATION))
    if alpha <= 0:
        loading_screen_fading = False
    else:
        fade_surface = _build_loading_surface(loading_screen_text, width, height)
        fade_surface.set_alpha(int(alpha))
        screen.blit(fade_surface, (0, 0))
```

This approach renders the race view (track, cars, stats, HUD) underneath the fade surface, creating a clean crossfade-like transition from loading screen into the live race view.

### Import Requirement

Add `import time` to the top of `src/tracksim/main.py`.

## Data and State Requirements

### New global state variables

```python
loading_screen_active: bool = False        # Whether the loading overlay is shown
loading_screen_text: str = ""              # Text displayed on the loading screen
loading_screen_start_time: float = 0.0     # time.time() when overlay appeared
loading_screen_fading: bool = False        # Whether the fade-out transition is active
loading_screen_fade_start: float = 0.0     # time.time() when fade-out began
MIN_LOADING_SECONDS: float = 3.0            # Minimum hold duration
FADE_DURATION: float = 0.5                  # Seconds for the fade-out animation
pending_race_start: bool = False           # Start a race after loading dismisses
```

### Import

```python
import time                                 # Wall-clock timer for loading duration
```

## Menu Requirements

No menu changes needed. The loading screen is triggered automatically by session mode flow. There is no menu toggle or configuration for the loading screen.

## Phase Flow

```txt
Session advancement decides next race
  → If session mode and not training_active:
      loading_screen_active = True
      loading_screen_fading = False
      loading_screen_text = context string (with race type, number, car type)
      loading_screen_start_time = time.time()
      pending_race_start = True
      (skip direct start_race_session() call)
  → Main loop frame:
      while loading_screen_active:
          → Event loop: check for early dismiss (input after 3s), continue past all else
          → Skip physics updates (continue to next frame without racing loop)
          → Render green overlay with text on top of everything
          → pygame.display.flip(); clock.tick()
          → Auto-dismiss check: if time.time() - start_time >= 3.0
              loading_screen_active = False
              loading_screen_fading = True
              loading_screen_fade_start = time.time()
      while loading_screen_fading:
          → Render full race view (track, cars, panes, HUD) underneath
          → Render loading surface on top at decreasing alpha (linear drop to 0 over 0.5s)
          → pygame.display.flip(); clock.tick()
          → If elapsed >= 0.5:
              loading_screen_fading = False
      if pending_race_start and not loading_screen_active and not loading_screen_fading:
          start_race_session(training=False)
          pending_race_start = False
```

## Non-Functional Requirements

1. No input events leak through the loading screen (clicks must not deselect cars or open menus).
2. The loading screen blocks the physics loop — no car simulation advances during the overlay.
3. The loading screen renders to the capture frame during ASR_STREAM mode (acceptable — shows a clean transition on the live stream).
4. Duration check uses `time.time()` wall clock, not sim time, so it reads consistently regardless of simulation speed.
5. The loading screen uses minimal CPU (just a filled rect and one text render per frame; the fade phase adds one alpha-blended surface blit per frame).
6. The fade-out transition renders at a smooth framerate — no stutter or jump from the alpha overlay.
7. During the fade-out phase, physics simulation runs normally (race view is live underneath).

## Acceptance Criteria

1. Loading screen appears before each qualifying race in session mode, showing the correct race number and car type.
2. Loading screen appears before each main series race in session mode, showing the correct race number.
3. Loading screen stays visible for at least 3 seconds.
4. A mouse click or keypress after 3 seconds dismisses the loading screen immediately.
5. Clicks during the loading screen (before 3s) are ignored entirely.
6. Loading screen covers all other UI elements (leaderboard, track, stats panes, menus).
7. Loading screen does not appear during Simulate (training) mode.
8. Loading screen does not appear for single-car type skip-qualifying advancement.
9. After dismissal, the loading screen fades out over ~0.5 seconds before the race starts.
10. During the fade-out, the race view (track, cars, HUD) is visible underneath the fading overlay.
11. After the fade completes, the race starts correctly with all cars in their qualifying-determined grid positions.

## Validation Checklist

- Start session mode with 2 types, 2 cars each, `qualifying_races=2`:
  - Confirm loading screen appears before qualifying race 1 (text: "Qualifying 1/4 -- Type: car").
  - Wait for race to finish → loading screen appears for qualifying race 2 ("Qualifying 2/4 -- Type: car").
  - Confirm loading screen appears before each subsequent qualifying race.
  - After all qualifying races, loading screen appears for main series race 1 ("Main Series Race 1").
- Verify loading screen cannot be dismissed before 3 seconds (clicks are ignored).
- Verify loading screen dismisses on click after 3 seconds.
- Verify loading screen auto-dismisses after 3 seconds with no input.
- Verify clicking during loading screen does not select a car or trigger a menu.
- Verify training/simulate mode does not show loading screens.
- Verify single-car-type skip-qualifying does not show a loading screen.
- Verify the fade-out transition: after 3s (or early dismiss), the green overlay fades out over ~0.5s revealing the race underneath.
- Verify the fade is visually smooth (no sudden disappearance, no alpha flicker).
- Verify physics simulation runs during the fade-out phase (cars are visible moving underneath).
- Verify the race starts immediately after the fade completes.

## Risks and Notes

1. **Timing precision**: The 3-second duration uses `time.time()` which is wall-clock time. A brief drift is acceptable since the requirement is "minimum" 3 seconds. The actual race start depends on frame timing after the loading flag is cleared.

2. **Deferred race start pattern**: The `pending_race_start` flag is checked in the main loop after loading dismissal. The session advancement code sets this flag and does not call `start_race_session()` directly. The main loop handles the actual call.

3. **Streaming compatibility**: During `ASR_STREAM=1`, the loading screen renders to the capture frame. This is desirable — it shows a clean green screen with race info as a transition before the race starts on the live stream.

4. **Event consumption**: During `loading_screen_active`, all event processing beyond the dismissal check is skipped via `continue`. This prevents any interaction from leaking through to the underlying UI state.

5. **Single-car types**: Loading screens do not appear for skip-qualifying advancement since no race simulation happens — points are awarded instantly. The skip handler should not set `loading_screen_active`.

6. **Clock source**: `time.time()` is used for the wall-clock timer. This is available in the standard library and does not require any pygame dependency.

7. **Fade transition implementation detail**: The fade surface must support per-pixel alpha or `set_alpha()`. Pygame's `Surface.set_alpha()` works on the whole surface and is sufficient for this use case. The surface should be created with `pygame.SRCALPHA` or `pygame.Surface((w, h), pygame.SRCALPHA)` so the green background and black text both fade uniformly. Rebuilding the fade surface each frame during the ~0.5s window (roughly 30 frames at 60fps) is acceptable for CPU cost.

8. **Streaming fade visibility**: During `ASR_STREAM=1`, both the solid loading screen and the fade-out are captured. This is desirable — viewers see a clean green splash → smooth dissolve → live race view, which is a polished broadcast transition.
