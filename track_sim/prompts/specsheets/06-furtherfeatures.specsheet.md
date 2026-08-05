# Specsheet: Bug Fixes and New Features

## Overview

Fix several bugs discovered in TrackSim (low waypoint density, duplicate stats windows, deselection not working during simulation) and implement new features (remove keyboard driving entirely, per-car unique starting waypoints, configurable lap limit for series races).

## Objective

Deliver a cleaner, raceable TrackSim that:

1. Generates a sufficient default waypoint density without requiring the user to press `+ Waypoint` repeatedly.
2. Displays race stats and car stats in a single location each (bottom stats pane), with working toggle buttons.
3. Deselects a selected car when clicking anywhere else on the track, including while the simulation is running.
4. Removes manual/keyboard driving completely — cars are always in auto mode and always driven by AI.
5. Gives each car a unique set of starting waypoints derived from its starting position, to reduce first-turn congestion and the regression where trailing cars crash.
6. Adds a configurable lap limit for series races, with series scoring based on fastest completion time of the defined lap count.

## Source Prompt

track_sim/prompts/06-furtherfeatures.prompt.md

## Prompt Review Findings

### Conflicts / Ambiguities in original prompt

1. **Car stats dropdown requirement**: The prompt says "have a dropdown menu to select which car to display stats for" without specifying UI style (pygame menu vs. in-pane selector). The default is the first car in the list. A simple in-pane dropdown/selector in the car stats section is the intended interpretation since the floating dropdown was previously removed.

2. **"Unique starting waypoints" vs. "update their waypoints as they drive"**: The prompt requires both an initial unique set (based on starting position) and dynamic updates during the race. The scope of dynamic updates (avoiding cars, wrecks, flying off track) is open-ended; the implementation should build on existing stall-recovery/recentering logic rather than a full path planner.

3. **Lap limit scoring wording**: The prompt says "Series scoring is based on the fastest time a car completes the defined number of laps." This means each race ends when the last unwrecked car completes the defined lap count, and finishing order (for points) is determined by each car's time to complete that lap count. Cars that never complete all laps are ranked behind completers. Note the existing scoring table `[5, 4, 3, 2]` + 1 point for completing matches the prompt's "5 points for first, 4 for second, 3 for third, 2 for fourth, 1 for completing the race."

4. **"Last unwrecked car completes the defined number of laps"**: If a car crashes before completing the lap limit, what happens? For scoring, it should receive ranking by laps completed / furthest progress, and only get the "1 point for completing" if it actually completed the defined lap count. Unwrecked cars that finish behind completers still get ranked normally.

### Resolution

1. Implement the car stats selector as an in-pane dropdown within the bottom car stats section.
2. Implement per-car waypoints as an initial seeding pass plus modest dynamic adjustment driven by existing stall/wreck/off-track recovery signals.
3. Add a `series_laps` config key; race ends when the last unwrecked car completes that many laps; scoring ordered by completion time (fastest first).
4. Rank crashed/non-completing cars by laps then distance; grant the completion point only to cars that finished the lap limit.

## Scope

### In scope

1. Verify/increase default waypoint density in `_build_default_route_plan` (already `max(20, min(60, n // 3))` in current code — confirm ≥20 typical).
2. Keep a single race stats display in the bottom pane, toggleable via `Toggle Race Stats`.
3. Keep a single car stats display in the bottom pane, toggleable via `Toggle Car Stats`.
4. Add a car-selector dropdown in the car stats panel (default = first car in list).
5. Fix clicking elsewhere on the track to deselect a selected car, including while the simulation is running.
6. Remove keyboard driving: remove manual mode toggle, remove the visual "AUTO (A)" indicator, remove keyboard inputs for driving.
7. Give each car a unique set of starting waypoints based on its starting position.
8. Add dynamic waypoint updates during driving (avoid cars, wrecks, off-track).
9. Add a configurable lap limit to series races via `tracksim.conf`.
10. Series scoring based on fastest completion time of the lap limit; points 5/4/3/2/1; accumulated across races; series winner = most points.

### Out of scope

1. New physics or AI behavior changes beyond waypoint updates.
2. Layout/UI redesign beyond the car-stats selector and removal of the AUTO indicator.
3. Training mode or infinite mode changes.
4. Track generation limit changes.
5. Car numbering/naming/color persistence in track files (already implemented in a prior pass; unchanged).

## Current Implementation Gaps

The following reflects the state of `src/tracksim/main.py` and `etc/tracksim.conf` at spec time. Items already implemented are documented for verification; items marked as gaps require new work.

### Bug: Waypoint density too low — ALREADY IMPLEMENTED

- **Source**: `_build_default_route_plan` in `src/tracksim/main.py`.
- **Current**: `target_count = max(20, min(60, n // 3))` — produces roughly 3x the previous density.
- **Action**: Verify default density is ≥20 on a typical track; document as satisfied.

### Bug: Race stats window showing twice — ALREADY RESOLVED

- **Current**: Floating race stats panel and `show_race_stats` bool are removed. Only `show_bottom_race_stats: bool = True` exists, gating the bottom-pane "Race Stats" section.
- **Menu action**: `Stats > Toggle Race Stats` toggles `show_bottom_race_stats` and reports "Race stats shown/hidden."
- **Action**: Verify no leftover duplicate; document as satisfied.

### Bug: Car stats window showing twice — PARTIALLY RESOLVED, GAP: dropdown

- **Current**: Floating car stats dropdown and `StatsDropdownState` are removed. Car stats render in the bottom pane gated by `show_bottom_car_stats: bool = True`.
- **Menu action**: `Stats > Toggle Car Stats` toggles `show_bottom_car_stats`.
- **Current behavior**: Stats follow `selected_car_index` (`stats_index = selected_car_index if selected_car_index is not None else 0`), defaulting to the first car.
- **Gap**: No dropdown to select which car to display stats for. Need an in-pane dropdown in the car stats section listing all cars; default = first car in the list; changing it re-targets the displayed stats.

### Bug: Clicking elsewhere does not deselect a car during simulation — GAP

- **Source**: Mouse click handler in `src/tracksim/main.py`.
- **Current**: Deselection is guarded by `if not racing:` — works when not racing, fails while the simulation is running.
- **Fix**: Remove the `racing` guard so clicking empty track space deselects even during the simulation. Ensure clicks on cars still select, and clicks on menu/picker UI do not deselect.

### Feature: Remove keyboard driving — PARTIALLY RESOLVED, GAP: AUTO indicator and A key

- **Current**: Arrow key handlers (`K_UP`/`K_DOWN`/`K_LEFT`/`K_RIGHT`) and manual throttle/brake/steering override are already removed.
- **Remaining**:
  - `autonomous_enabled = True` variable and the `pygame.K_a` toggle handler.
  - The `if autonomous_enabled:` gate around `autonomous_controls(...)` in the simulation loop.
  - The `mode_line = "AUTO (A)" if autonomous_enabled else "MANUAL (A)"` rendered in the leaderboard pane.
  - Message strings: "Autonomous mode on." / "Manual mode on."
  - Help message text: `"... A toggle auto ..."`.
- **Fix**: Remove manual mode toggle entirely. Cars always use `autonomous_controls(...)` unconditionally. Remove the visual "AUTO (A)" indicator and all related messaging.

### Feature: Unique per-car starting waypoints — GAP

- **Current**: All cars receive the same route plan from `_load_route_plan_from_track`, which loads from `track.metadata["car_routes"]` or falls back to `_build_default_route_plan(track)` — a single shared centerline-based waypoint set. This causes the regression where all cars follow the same path and the first turn congests, so back-of-grid cars crash or fall behind.
- **Fix**:
  1. When building/loading a route plan for a car, seed the waypoint set from the car's starting grid position `entry.start_pose` (via a per-car offset or per-car centerline sampling), so each car has a unique layout of its most efficient path.
  2. Persist per-car routes per car instance (already supported by `car_routes` keyed by instance name in track metadata).
  3. Cars update their waypoints as they drive to optimize their path — avoiding cars, wrecks, and flying off track. Build on existing mechanisms: `_seed_route_target_from_pose`, `_normalize_route_order`, stall-recovery, and hard-recenter logic rather than a full planner.
- **Note**: `Waypoint` model already supports `source` and `created_lap` fields for dynamic waypoints; may add a per-car lateral offset field or reuse start pose.

### Feature: Configurable lap limit to series races — GAP

- **Current**: Race ends only when all cars are crashed (`all_stopped`) or via `Quit Race`. No lap limit end condition.
- **Current config**: `tracksim.conf` has `series_name`, `series_races`, `series_logo`. Parsed as `series_name = as_str(conf, "series_name", "")`, `series_race_target = max(0, as_int(conf, "series_races", 0))`, `series_logo_path = as_str(conf, "series_logo", "")`.
- **Fix**:
  1. Add a new config key, e.g. `series_laps`, to `etc/tracksim.conf` and parse it (e.g., `series_lap_limit = max(1, as_int(conf, "series_laps", 3))`).
  2. During a race, when the last unwrecked car completes the defined number of laps, set `racing = False` and save the outcome.
  3. Finishing order for points is determined by the time each car took to complete the defined lap count (fastest first). Cars that did not complete the lap count are ranked by laps completed, then distance.
  4. Scoring: 5 first, 4 second, 3 third, 2 fourth, 1 for completing the race (already present in `_award_series_points` with `points_table = [5, 4, 3, 2]` and +1 completion point — update ordering logic to lap-limit completion time).
  5. Points accumulate across races; series winner = car with most points at the end of the series (already handled by `CarSeriesStats.points` and final winner selection).

## Data and State Requirements

### Configuration (`etc/tracksim.conf`)

Add a new key (example value shown):

```ini
# series_laps: number of laps required to complete a series race (last unwrecked car finishing this many laps ends the race)
series_laps=3
```

Parsed in `src/tracksim/main.py` alongside `series_races`/`series_logo`:

- `series_lap_limit: int = max(1, as_int(conf, "series_laps", 3))`

### Route planning (`src/common/models.py`)

No required model changes. Options considered:

- `CarRoutePlan` and `Waypoint` already support per-car storage, `source`, and `created_lap` fields — sufficient for unique seeded waypoints and dynamic updates.
- If lateral per-car offset is desired for unique starting paths, a new field on `CarRoutePlan` (e.g., `lateral_offset: float = 0.0`) may be added, serialized via track metadata.

### State variables (`src/tracksim/main.py`)

Removals:
- `autonomous_enabled: bool = True`
- `pygame.K_a` key handler
- `mode_line` / `mode_color` rendering in the leaderboard pane
- "Autonomous mode on." / "Manual mode on." messages
- `if autonomous_enabled:` gate around `autonomous_controls(...)`

Additions:
- `series_lap_limit: int` (from config)
- Car-stats selector state (e.g., `stats_dropdown_open: bool`, `stats_selected_index: int` or reuse `selected_car_index` defaulting to first car)

### Series scoring

- Points table stays `[5, 4, 3, 2]` for 1st-4th; 1 point for completing the race.
- "Completing the race" = finishing the defined lap limit.
- Ordering within `_award_series_points` changes from laps/distance to: completion time of the lap limit (fastest first) for completers, then laps/distance for non-completers.

## Functional Requirements

### Fix: Waypoint Density

1. Verify `_build_default_route_plan` keeps `max(20, min(60, n // 3))` (already implemented).
2. Confirm a freshly loaded typical track shows at least 20 waypoints per car.

### Fix: Remove Duplicate Race Stats Panel

1. Verify no floating race stats panel exists; only the bottom-pane "Race Stats" section remains.
2. `Stats > Toggle Race Stats` must toggle `show_bottom_race_stats`.

### Fix: Remove Duplicate Car Stats Panel + Add Selector

1. Verify no floating car stats panel exists; only the bottom-pane "Car Stats" section remains.
2. Add an in-pane dropdown/selector in the car stats section listing all loaded cars.
3. Default selection is the first car in the list.
4. Selecting a car in the dropdown changes which car's stats are displayed.
5. `Stats > Toggle Car Stats` must toggle `show_bottom_car_stats`.

### Fix: Deselect Car on Click Elsewhere (Including During Simulation)

1. Remove the `if not racing:` guard on the deselection path in the mouse click handler.
2. Clicking empty track space deselects the currently selected car whether or not the simulation is running.
3. Clicking a car still selects it; clicks on menus/pickers do not deselect.

### Feature: Remove Keyboard Driving

1. Remove `autonomous_enabled` variable and the `A` key (`pygame.K_a`) handler.
2. Remove the visual `"AUTO (A)"` / `"MANUAL (A)"` mode line from the leaderboard pane.
3. Call `autonomous_controls(...)` unconditionally in the simulation loop.
4. Update the help/message strings that reference "A toggle auto".
5. Cars are always in auto mode and always driven by AI.

### Feature: Unique Per-Car Starting Waypoints

1. Each car gets its own unique set of starting waypoints derived from its starting grid position.
2. Seeding uses the car's `start_pose` (e.g., `_seed_route_target_from_pose` and/or a per-car offset when building the default route) so back-of-grid cars don't all follow the identical centerline path.
3. Per-car routes persist to track metadata under `car_routes` keyed by instance name (already supported).
4. While driving, cars update their waypoints to optimize their path: avoiding cars, wrecks, and flying off track (build on stall recovery, hard recenter, and route normalization).
5. Verify first-turn congestion is reduced: cars in the back are more likely to complete laps.

### Feature: Configurable Lap Limit to Series Races

1. Add `series_laps` to `etc/tracksim.conf` and parse it in `src/tracksim/main.py`.
2. A series race ends when the last unwrecked car completes the defined number of laps.
3. Finishing order for scoring is by fastest completion time of the lap limit.
4. Points: 5/4/3/2/1 as specified; accumulated across races.
5. Series winner = car with most points at the end of the series.

## Non-Functional Requirements

1. Backward-compatible config: missing `series_laps` defaults to a sensible value (e.g., 3).
2. Backward-compatible track loading: track files with existing `car_routes` load correctly; missing routes fall back to per-car unique default plans.
3. No performance regression from per-car dynamic waypoint updates (should reuse existing per-tick logic).
4. Menu items remain functional and toggle the correct bottom pane sections.
5. No keyboard input affects car control; the simulation is always autonomous.

## Acceptance Criteria

1. Default waypoint density is ≥20 on a typical freshly loaded track.
2. Only one race stats display exists — in the bottom pane — toggleable via `Toggle Race Stats`.
3. Only one car stats display exists — in the bottom pane — toggleable via `Toggle Car Stats`.
4. The car stats panel has a dropdown to select which car's stats to display; defaults to the first car.
5. Clicking empty track area deselects the selected car, both when not racing and while the simulation is running.
6. No manual mode exists: no arrow key effects, no `A` key toggle, no "AUTO (A)" indicator, no manual/auto message.
7. Every car has a unique set of starting waypoints based on its starting position.
8. Cars update waypoints while driving to avoid cars, wrecks, and off-track excursions.
9. First-turn congestion is reduced; back-of-grid cars complete laps at a noticeably higher rate than before.
10. A `series_laps` key in `tracksim.conf` controls when a series race ends.
11. Series races end when the last unwrecked car completes the configured lap count.
12. Series scoring uses fastest completion time ordering with 5/4/3/2/1 points; points accumulate and the series winner has the most points.

## Validation Checklist

- Load a track and check per-car waypoint counts — should be at least 20 per car on a typical track.
- Verify each car's waypoints are unique/differ by starting position (visualize waypoint circles per car).
- Open Stats menu, toggle Race Stats — confirm only the bottom pane race stats section appears/disappears.
- Open Stats menu, toggle Car Stats — confirm only the bottom pane car stats section appears/disappears.
- Use the car stats dropdown — confirm it lists all cars, defaults to the first car, and switches displayed stats.
- Click a car to select it, then click empty track space — confirm deselect works while paused.
- Start a race, select a car, then click empty track space — confirm deselect works while the simulation is running.
- During a race, press arrow keys and `A` — confirm no car responds and no mode indicator appears.
- Run a series race with `series_laps=3` — confirm the race ends when the last unwrecked car completes 3 laps.
- Confirm series points order matches fastest completion time, and the series winner has the most points after all races.

## Risks and Notes

1. Dynamic per-car waypoint updates are the riskiest feature. Keep updates conservative (reuse existing stall/recovery/recenter signals) to avoid destabilizing the tuned control loop.
2. Seeding unique waypoints may change handling characteristics per car; verify all cars can still complete laps and that straight-line pacing is not degraded.
3. The lap-limit end condition changes race length behavior. Verify series race duration is reasonable with the default lap count.
4. Scoring by completion time requires tracking each car's time at the moment it crosses the lap limit; non-completers rank by laps/distance.
5. Existing track files without per-car seeded routes should load fine and receive unique defaults generated at load time.