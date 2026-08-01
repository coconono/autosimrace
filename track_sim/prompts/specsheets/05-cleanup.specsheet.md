# Specsheet: UI Clean Up, Race Series, and Feature Improvements

## Overview

Polish TrackSim for streaming by fixing Simulate mode race completion, reorganizing the UI into dedicated panes (track, leaderboard, stats), adding a multi-race series model with points and logo support, tuning AI line-learning behavior, and introducing an infinite-race flow with hot-reloadable car names.

## Objective

Deliver a streaming-ready TrackSim that:

1. Completes races reliably in Simulate (training) mode.
2. Uses pygame menus with concise labels and variable-size fonts to prevent text overruns.
3. Separates the track into its own pane with a real-time leaderboard side pane and a stats bottom pane.
4. Introduces a race series (multiple races with accumulated points and a series logo).
5. Removes the leader line-offset freeze and broadens AI lateral line learning.
6. Runs an infinite race loop that resets when all cars wreck and re-reads car names before each race.

## Source Prompt

- Fix Simulate mode: increasing simulation speed causes cars to fail to complete laps.
- Implement pygame menus.
- Rename `Increase Waypoint Density` to `+ Waypoint`; rename `Decrease Waypoint Density` to `- Waypoint`.
- Use variable-size fonts so text fits dropdown menus and info boxes.
- Separate the track into its own pane.
- Add a real-time leaderboard side pane.
- Add a bottom stats pane with lap times, best times, etc.
- Follow `track_sim/images/layout/RaceSimLayout.drawio.png` for pane layout.
- Series stats 1: series name, number of races, fastest lap of series (car, race, lap time, lap number), slowest lap of series (car, race, lap time, lap number).
- Series stats 2: top 5 points leaders.
- Race stats 1: race leader, top speed, laps completed, fastest lap time, slowest lap time.
- Race stats 2: most time spent in a single drift (car name and time), quickest to crash (car name and time), last car to hit another car (car name and time).
- Car stats pane remains the same other than positioning.
- `tracksim.conf` gains series name, number of races, and series logo file path.
- Points: 5 for 1st, 4 for 2nd, 3 for 3rd, 2 for 4th, 1 for completing the race.
- Series winner is the car with the most points at the end of the series.
- Series logo is displayed in the series stats pane; size requirements documented in `README.md`.
- Remove the logic that makes the leader stop adjusting waypoints.
- Increase the amount of lateral line offset the AI can learn; current range is too small.
- Infinite loop: when all cars wreck, reset to a new race.
- Re-read car names before each race so SSH/vim edits appear next race.

## Prompt Review Findings

### Conflicts / Ambiguities in original prompt

1. "Implement pygame menus" while menus already exist as custom dropdowns in `src/common/ui.py` and `src/tracksim/main.py`. Intent is to refine/replace with proper pygame menu widgets and fix overruns, not add a second menu system.
2. "Remove the logic that makes the leader stop adjusting waypoints" conflicts with the prior specsheet (`04-tuning-LineFollowing`) which intentionally freezes leader line-offset adaptation. Resolution: remove the leader freeze specifically; keep other line-following behavior intact.
3. "Infinite loop" vs existing training mode that runs a fixed number of races. Resolution: introduce a continuous series loop mode distinct from the fixed-count training mode, or extend training to support an unbounded series.
4. "Re-read car names" implies reloading car files from disk before each race; current code caches `SimCar` objects and only reloads on explicit Load Car. Resolution: reload car configs from their source files at race reset.

### Resolution

1. Treat menu work as a UI overhaul: adopt pygame menu widgets (or refined custom widgets) with dynamic font sizing and shorter labels.
2. Remove `line_offset_frozen` leader gating and allow all cars to continue adapting.
3. Add a series/infinite mode that resets races indefinitely and reloads car configs each reset.
4. Reload car files from `cars_dir` using each `SimCar.source_file` before starting a new race.

## Scope

### In scope

1. Simulate mode timing/physics fix for race completion.
2. Menu/widget overhaul with variable-size fonts and renamed waypoint density labels.
3. Pane-based layout: track pane, leaderboard side pane, stats bottom pane (series + race + car).
4. Series data model: series name, race count, points, logo path, series-level fastest/slowest laps, points leaders.
5. `tracksim.conf` additions for series configuration.
6. Series logo loading and rendering; README documentation of size requirements.
7. Race stats expansion: drift duration, quickest crash, last car-to-car contact.
8. AI tuning: remove leader line-offset freeze; widen lateral line-offset learning range.
9. Infinite race loop with automatic reset when all cars wreck.
10. Hot-reload car names/configs from disk before each new race.

### Out of scope

1. Network/multiplayer synchronization.
2. Full replay system.
3. New physics model beyond the Simulate timing fix.
4. ML/NN training systems.

## Review Findings (Current Implementation Gaps)

### High Severity

1. Simulate mode race completion is broken.
   - `training_speed_multiplier = 100.0` in `src/tracksim/main.py` multiplies `dt`, which is then passed into `update_car_state` and `autonomous_controls`.
   - Large `dt` steps cause control/physics instability (overshoot, missed waypoints, lap-line skips), preventing lap completion.
   - `dt = min(clock.get_time() / 1000.0, 0.05)` caps real-time dt, but after `dt *= training_speed_multiplier` it can reach ~5.0s per frame, far above stable simulation step.

2. No race series model exists.
   - `tracksim.conf` has no series name, race count, or logo fields.
   - No points accumulation, series fastest/slowest lap tracking, or points-leader state in `SimCar` or models.

3. No infinite/reset loop for wrecked fields.
   - When `all_stopped` is true, `racing = False` and the message says "Press N to restart"; no automatic reset occurs.

### Medium Severity

1. UI pane layout does not match the layout image.
   - Track is drawn full-screen; stats/debug/race panels float at bottom-right.
   - No dedicated track pane, leaderboard side pane, or structured bottom stats pane.

2. Text overruns in menus/info boxes.
   - `draw_dropdown_menus` and `draw_lines` use a fixed font size; long labels like `Increase Waypoint Density` can overflow.
   - No variable-size font fitting logic exists.

3. Leader line-offset freeze is still active.
   - `entry.line_offset_frozen` is set for the current leader in `src/tracksim/main.py` and guards adaptation in the lap-completion block.

4. Lateral line-offset learning range is too small.
   - `max_offset = max(6.0, car.width * 0.95)` and adaptation deltas are scaled by `car.line_offset_scale` (default 0.35), limiting explored lines.

### Low Severity

1. Car names are not hot-reloaded.
   - `SimCar` objects persist across races; `source_file` is stored but not re-read on reset.

2. Series logo support is absent.
   - No logo loading/rendering code; `images/logos/` directory exists but is unused.

## Data and State Requirements

### Configuration (`etc/tracksim.conf`)

Add keys (with defaults for backward compatibility):

1. `series_name` (string, default `""`).
2. `series_races` (int, default `0`; `0` means use existing `training_races` behavior or infinite mode).
3. `series_logo` (string, default `""`; relative path under `images/logos/` or absolute path).

### Runtime state

In `SimCar` (`src/tracksim/main.py`) and/or new series state:

1. `series_points: int = 0` per car.
2. `series_fastest_lap` / `series_slowest_lap` records: car name, race number, lap time, lap number.
3. `race_number: int` (current race index within the series).
4. Drift tracking: `max_drift_duration: float` and `drift_start_time: float` (or equivalent) to compute most time spent in a single drift.
5. Crash timing: `first_crash_time: float | None` for quickest-to-crash.
6. Contact tracking: `last_contact_time: float | None` and `last_contact_car: str | None` for last car-to-car contact.
7. Remove `line_offset_frozen: bool` usage for leader gating (field may remain for debug display but must not block adaptation).

### Models (`src/common/models.py`)

1. Add `CarSeriesStats` (points, fastest/slowest lap info) or extend `CarRaceMemory`.
2. Add series-level lap record dataclass: `SeriesLapRecord(car_name, race_number, lap_time, lap_number)`.

## Functional Requirements

### Simulate Mode Fix

1. Simulate (training) mode must complete races at increased speed.
2. Decouple simulation speed from render frame `dt`:
   - Run physics/AI in fixed sub-steps with a capped per-step `dt` (for example, iterate N sub-steps of `<= 0.05s` per rendered frame).
   - Alternatively, scale speed via sub-step count, not by inflating `dt` beyond stable limits.
3. Lap counting (`update_lap_counter`) and waypoint advancement (`advance_if_reached`) must remain accurate under accelerated simulation.
4. Verify cars can complete multiple laps and finish races at `training_speed_multiplier` values up to at least 100x.

### Menus and Text Fitting

1. Replace or refine menu rendering with pygame menu widgets (or improved custom widgets) that support:
   - Hover/click states (already present).
   - Dynamic font sizing to fit item width.
2. Rename menu items:
   - `Increase Waypoint Density` -> `+ Waypoint`.
   - `Decrease Waypoint Density` -> `- Waypoint`.
3. Implement variable-size font helper:
   - Given target width/height and text, select the largest font size that fits.
   - Apply to dropdown items and info-box lines.
4. Ensure no text overruns in dropdown menus or info boxes at the configured window size.

### Pane Layout

1. Implement pane layout per `track_sim/images/layout/RaceSimLayout.drawio.png`:
   - Track pane: dedicated region for track rendering and cars.
   - Leaderboard side pane: real-time standings (position, car name, laps, gap, points).
   - Bottom stats pane: series stats 1, series stats 2, race stats 1, race stats 2, and car stats.
2. Track rendering must be clipped/scaled to the track pane; cars and overlays draw within it.
3. Panes must not overlap the track pane; layout must be stable at 1600x900.
4. Car stats pane content remains the same; only positioning changes.

### Leaderboard Side Pane

1. Show live race order by laps then distance traveled.
2. Display each car's position, name, laps completed, and gap to leader (optional: points total).
3. Update in real time each frame.

### Series Stats Pane 1

1. Series name.
2. Number of races (configured total and completed count).
3. Fastest lap of the series: car name, race number, lap time, lap number.
4. Slowest lap of the series: car name, race number, lap time, lap number.
5. Display series logo (if configured) within this pane.

### Series Stats Pane 2

1. Top 5 points leaders with points totals.

### Race Stats Pane 1

1. Race leader.
2. Top speed (car name and speed).
3. Laps completed (leader laps).
4. Fastest lap time (current race).
5. Slowest lap time (current race).

### Race Stats Pane 2

1. Most time spent in a single drift: car name and duration.
2. Quickest to crash: car name and time-to-crash.
3. Last car to hit another car: car name and time.

### Race Series

1. `tracksim.conf` provides `series_name`, `series_races`, and `series_logo`.
2. Points awarded per race: 5/4/3/2/1 for 1st-4th, plus 1 for completing the race.
3. Points accumulate across races in `series_points` per car.
4. Series winner is the car with the most points after the configured number of races.
5. Series fastest/slowest lap records are tracked across all races in the series.
6. Series stats panes display the data described above.

### Series Logo

1. Load logo from path in `series_logo` (relative to project dir or `images/logos/`).
2. Render within series stats pane 1.
3. Size requirements determined by pane layout; document required dimensions in `README.md`.
4. Missing/invalid logo path must not crash; show placeholder or omit gracefully.

### AI Tuning

1. Remove leader-based line-offset freeze:
   - Do not set `line_offset_frozen` based on leadership.
   - All cars continue adapting `preferred_line_offset` after lap completion.
2. Increase lateral line-offset learning range:
   - Raise `max_offset` (currently `max(6.0, car.width * 0.95)`).
   - Increase adaptation deltas or `line_offset_scale` influence so cars explore wider lines.
   - Keep safety constraints (track surface, barrier avoidance) as hard overrides.

### Infinite Race Loop and Hot Reload

1. When all cars are wrecked/stopped, automatically reset and start a new race (infinite mode).
2. Before each new race, re-read car configs from disk using `SimCar.source_file`:
   - Reload `CarConfig` from `cars_dir / source_file`.
   - Update `instance_name` from the reloaded config (so edited names appear next race).
   - Preserve series points and series lap records across resets.
3. Provide a way to exit the infinite loop (for example, Quit Race or ESC).
4. Training mode with a fixed race count may remain separate; infinite mode applies to series/streaming flow.

## Non-Functional Requirements

1. Maintain frame responsiveness at 1600x900 with multiple cars and panes visible.
2. Backward-compatible config load: missing series keys use defaults.
3. Backward-compatible car/track file load: old files continue to work.
4. Keep code modular: series state, pane rendering, menu rendering, and AI tuning are separate concerns.

## Acceptance Criteria

1. Simulate mode completes full races at 100x speed without cars stalling or missing laps.
2. Menus show `+ Waypoint` and `- Waypoint`; no text overruns in menus or info boxes.
3. Track renders in its own pane; leaderboard and stats panes are visible and non-overlapping per the layout image.
4. Leaderboard updates in real time with correct ordering.
5. Series stats pane 1 shows series name, race counts, fastest/slowest series lap details, and logo (when configured).
6. Series stats pane 2 shows top 5 points leaders.
7. Race stats pane 1 shows leader, top speed, laps completed, fastest/slowest lap.
8. Race stats pane 2 shows longest single drift, quickest crash, last car-to-car contact.
9. `tracksim.conf` supports `series_name`, `series_races`, `series_logo`.
10. Points accumulate correctly across races; series winner is highest points after final race.
11. Leader no longer freezes line-offset adaptation; all cars adapt.
12. Cars explore visibly wider lateral line offsets over laps.
13. When all cars wreck, a new race starts automatically; car names/configs reload from disk each reset.
14. `README.md` documents series logo size requirements.

## Validation Checklist

- Run Simulate at 100x and confirm multiple laps complete and race finishes.
- Verify menu labels and that long labels fit with variable font sizing.
- Confirm pane layout matches `RaceSimLayout.drawio.png` at 1600x900.
- Load a series config and verify points accumulate across at least 3 races.
- Edit a car file mid-series and confirm new name appears after the next automatic reset.
- Confirm series fastest/slowest lap records update and display correctly.
- Verify leaderboard ordering matches laps/distance logic.
- Confirm race stats 2 values populate (drift, crash, contact) during a race.
- Verify leader car continues adapting line offset after becoming leader.
- Confirm wider line offsets are visible in selected-car overlay over successive laps.
- Verify missing series logo does not crash.

## Risks and Notes

1. Fixed sub-stepping for Simulate mode may increase CPU cost; keep sub-step count bounded.
2. Pane layout may require track scaling/translation; ensure car selection and drag coordinates map correctly to the track pane.
3. Removing leader freeze may increase variance for leading cars; monitor race stability.
4. Wider line-offset learning may increase wall contact on narrow tracks; retain barrier avoidance guards.
5. Hot-reloading car configs must not reset series points or series lap records.
6. Infinite loop must remain cancellable to avoid unattended runaway processes.
