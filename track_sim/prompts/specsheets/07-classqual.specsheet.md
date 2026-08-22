# Specsheet: Class Qualification & Session Mode

## Overview

Replace TrackSim's infinite mode with a session-based race format where car classes/types independently qualify to set the grid for a main series race. Points accrue through both qualifying and main series races, and the series stats pane gains context indicators showing the current race phase (qualifying vs. main series), which car type is qualifying, and the qualifying/main race number.

## Objective

Deliver a session-mode race flow that:

1. Replaces the "Infinite Mode" menu entry and logic with "Session Mode".
2. Groups cars by type (unique car config file) for independent qualifying races.
3. Runs one qualifying race per car type, where the winner is sent to the back of that type's grid (and all others move up one position), establishing a final grid for the main series.
4. Awards series points during qualifying races (same points table as the main series).
5. Transitions to the main series (all car types together) after all qualifying races complete.
6. Updates the series stats pane to display race context: the car type during qualifying, qualifying race number, or main series race number.

## Source Prompt

`track_sim/prompts/07-classqual.prompt.md`

## Prompt Review Findings

### Conflicts / Ambiguities in original prompt

1. **"Each type of car" is now clarified by the prompt.** Cars are defined by `.car` files in `/tracksim/cars/` and instanced onto maps. The type name is the `.car` filename stem (e.g., `car.car` → type "car", `car2.car` → type "car2"), not the instance name on the map. Example from prompt: instance `CloverFPV` is of type "car" (loaded from `car.car`), instance `car2_2` is of type "car2" (loaded from `car2.car`).
2. **"Series of qualifying races" vs. "winner goes to back"**. The prompt says each type will have "a series of qualifying races" but the described mechanic (winner→back, rest move up one) is a single-race grid-reset scheme. Running multiple qualifying races per type with a winner-goes-to-back on each iteratively reorders the grid. Resolution: treat "a series of qualifying races" as **`qualifying_races` races per car type** (configurable in `tracksim.conf`, default 1). Each race within a type reorders that type's grid independently, and the grid after the final race is the starting grid for the main series.

3. **"Defined in tracksim.conf under series configuration" — no new configuration keys specified.** The prompt says qualifying is "defined in tracksim.conf under series configuration" but does not enumerate specific new keys. Existing `series_name`, `series_races`, `series_laps` already control the series. The car type grouping can be derived from source files — no new config keys are needed for basic functionality. However, a `session_mode=1` flag (default 0) should be added to enable session mode vs. classic series mode. A `qualifying_laps` key (default = `series_laps`) may be useful.

4. **How do different car types interact on track?** Prompt says "each type of car will have a series of qualifying races" — natural reading is that **each type qualifies independently** (only same-type cars on track). This avoids inter-type interference during qualifying. During the main series, all types race together.

5. **Series points during qualifying**: Points accrued during qualifying count toward the same series total as main series points. The `series_races` config key should apply to **main series races only** (qualifying is additional).

6. **Series stats pane wording**: Needs a clear indicator (e.g., "Session: Qualifying (1/3)" or "Session: Main Race 1"). The pane should also show which car type is qualifying when in a qualifying race.

### Resolution

1. **Car type** = unique `source_file` stem among loaded `SimCar` instances. Cars loaded from the same `.car` config file are the same type. The type label is the `.car` filename stem (e.g., `car.car` → "car", `car2.car` → "car2"). The instance name on the map (e.g., `CloverFPV`, `car2_2`) is separate from the type name.

2. **One qualifying race per type** (or `qualifying_races` per type when configured). Each type runs `qualifying_races` lap-limited races with only its own cars on track. After each race, the winner moves to the back and all others advance one position. The grid after the final qualifying race for a type is the final grid for that type in the main series.

3. **New config keys**:
   - `session_mode` (`0`/`1`): Enables session mode (qualifying + main series). Default `0` preserves backward-compatible behavior.
   - `qualifying_laps` (int): Lap limit for each qualifying race. Default = `series_laps` (or 3 if `series_laps` is 0).
   - `qualifying_races` (int): Number of qualifying races per car type. Default `1`.

4. **Independent qualifying**: Each type qualifies alone on an empty track. Inter-type cars are removed from the simulation during that type's qualifying race.

5. **Series points**: Awarded per race (qualifying or main series) using the existing `_award_series_points` table `[5, 4, 3, 2]` + 1 point for completing. `series_races` counts only main series races; qualifying races are additional.

## Scope

### In scope

1. Add `session_mode` boolean state (driven by new config key or UI toggle).
2. Replace "Infinite Mode" menu item with "Session Mode" in the Race dropdown.
3. Implement car-type grouping by unique `source_file`.
4. Implement qualifying race flow per car type (winner-to-back grid reset).
5. Accumulate series points during qualifying.
6. Transition to main series (all types together) after all qualifying races complete.
7. Series stats pane update: session context line.
8. Config keys: `session_mode`, `qualifying_laps` in `tracksim.conf`.
9. Backward-compatible: existing series behavior unchanged when `session_mode=0` (default).

### Out of scope

1. Multi-class racing (different car types in the same race with class-based scoring).
2. Qualifying elimination rounds (multiple heat races per type).
3. Track-side qualifying timing display (sector times, pole position marker).
4. Changes to existing training/simulate mode.
5. Car class/type tagging in car editor.

## Functional Requirements

### Session Mode Entry Point

1. The "Race" dropdown menu item "Infinite Mode" is renamed to "Session Mode".
2. Selecting "Session Mode" when a race is active shows a message "Quit current race before starting session mode."
3. Selecting "Session Mode" when no race is active:
   - Resets series state (`CarSeriesStats` per car).
   - Sets `session_active = True`, `series_completed_races = 0`, `series_race_number = 0`.
   - If any cars are loaded, starts the qualifying phase automatically.
   - If no cars are loaded, shows a message "Load at least one car first."

### Car-Type Grouping

1. Cars are grouped by `source_file` stem (the `.car` filename without extension).
2. Cars loaded from the same `.car` file share the same type regardless of their instance name on the map. Example: instances `CloverFPV` and `CloverFPV_2` both loaded from `car.car` are both type "car". Instance `car2_2` loaded from `car2.car` is type "car2".
3. The type display name is the `.car` filename stem (e.g., "car", "car2").
4. If only one car exists of a given type, that type's qualifying race is a single-car "race" — the car automatically finishes first (and is moved to the back, which is a no-op for a single car). No actual simulation runs for single-car types.

### Qualifying Phase

1. Qualifying runs `qualifying_races` races per car type, processing types in the order they were loaded. If `qualifying_races > 1`, the types cycle: all of Type A's races run first, then all of Type B's, etc.
2. Each qualifying race:
   - Only cars of the current type are active on track (other types are removed/hidden).
   - Cars start in their original map-defined positions.
   - The race uses a lap limit (`qualifying_laps` from config, default = `series_laps` or 3).
   - The race ends when the last unwrecked car completes the lap limit (same logic as current series lap-limit ending).
   - Series points are awarded via `_award_series_points` after each qualifying race.
3. After each qualifying race completes:
   - **Winner moves to the back of that type's grid** (position is swapped to last among that type).
   - **All other cars of that type advance one position** (their start grid order moves up by one).
   - The new start poses are recorded for the main series.
4. Qualifying race number increments for each type (starting at 1).
5. The series stats pane shows "Session: Qualifying (N/M) — Type: <type_name>" during qualifying races, where `<type_name>` is the `.car` filename stem of the current qualifying type.

### Main Series Phase

1. After all qualifying races complete, the main series begins.
2. All cars (all types) start from their qualifying-determined grid positions.
3. The main series runs according to existing `series_races`, `series_laps` config.
4. Series points continue to accrue.
5. The series stats pane shows "Session: Main Race N" where N is the race number within the main series (1-based).
6. If `series_races == 0`, the main series runs indefinitely (infinite loop within session mode).
7. When the configured number of main series races is completed, the series ends and a winner is declared (same as existing series completion logic).

### Series Stats Pane

1. Add a "Session" line at the top of series stats pane 1 (above the series name):
   - During qualifying: `"Session: Qualifying (N/M) — Type: <type_name>"` where N = current qualifying race number (1-based), M = total number of qualifying races (= number of car types), `<type_name>` = the `.car` filename stem of the type currently qualifying.
   - During the main series: `"Session: Main Race N"` where N = current main series race number (1-based).
   - When not in session mode (classic series/infinite): no session line displayed.
2. The series name, race counts, fastest/slowest lap records continue to display below the session line.

## Data and State Requirements

### New global state variables (in tracksim main loop)

```
session_active: bool = False              # Session mode active
session_qualifying: bool = False          # True during qualifying phase
session_main_series: bool = False         # True during main series phase
session_qualifying_count: int = 0         # Number of qualifying races run
session_qualifying_total: int = 0          # Total qualifying races (= car type count)
session_qualifying_index: int = 0          # Current qualifying race index (0-based)
session_main_race_count: int = 0          # Current main series race number (1-based)
```

### SimCar / per-car state

No new fields needed on `SimCar`. The existing `series_stats` (points, lap records) and start pose handling are sufficient. The grid reorder after qualifying is a rearrangement of start poses, not new per-car state.

### Config defaults

```python
session_mode = as_int(conf, "session_mode", 0)       # 0 = classic, 1 = session mode
qualifying_laps = as_int(conf, "qualifying_laps", 0)  # 0 = use series_laps
qualifying_races = as_int(conf, "qualifying_races", 1)  # races per car type
```

If `qualifying_laps == 0`, fall back to `series_laps`. If `series_laps == 0`, fall back to 3.

## Menu Requirements

1. The "Race" dropdown menu entry "Infinite Mode" is replaced with "Session Mode".
2. The menu definition changes from:

   ```python
   ("Race", ["Start Race", "Simulate", "Start Series", "Infinite Mode", ...])
   ```

   to:

   ```python
   ("Race", ["Start Race", "Simulate", "Start Series", "Session Mode", ...])
   ```

3. The menu action handler for "Session Mode" dispatches to either the qualifying flow (`session_mode=1`) or the old infinite-mode flow (`session_mode=0`).

## Phase Flow State Machine

```
IDLE → (user clicks "Session Mode")
         ↓
    session_mode=1?  ──NO──→ [old infinite-mode logic: all-wreck loop]
         ↓YES
    [QUALIFYING PHASE]
    for each car type (repeat `qualifying_races` times):
        run qualifying race (type only)
        award series points
        reorder grid: winner→back, rest up
        session_qualifying_count++
    ↓
    [MAIN SERIES PHASE]
    run main series races (all types)
    series points continue
    until series_races exhausted or infinite
    ↓
    [SERIES COMPLETE] declare winner
```

## Config (`tracksim.conf`)

1. Add `session_mode` (integer 0/1, default 0):
   - `session_mode=1` enables the full qualifying + main series flow when "Session Mode" is selected.
   - `session_mode=0` preserves old infinite-mode behavior (all-wreck reset loop with hot reload).
2. Add `qualifying_laps` (integer, default = `series_laps` or 3):
   - Lap limit for each qualifying race.
   - Only used when `session_mode=1`.
3. Add `qualifying_races` (integer, default = 1):
   - Number of qualifying races per car type.
   - Only used when `session_mode=1`.

## Non-Functional Requirements

1. Backward-compatible config: missing `session_mode` and `qualifying_laps` keys use defaults (0).
2. Backward-compatible behavior: when `session_mode=0`, "Session Mode" menu item behaves exactly like old "Infinite Mode".
3. Existing series stats pane layout is preserved; the session line is additive.
4. Qualifying races run in accelerated sim speed (same as training/simulate mode) when `ASR_STREAM=1` or headless.
5. No performance regression: qualifying races for single-car types skip simulation entirely.

## Acceptance Criteria

1. "Session Mode" menu entry replaces "Infinite Mode" in the Race dropdown.
2. When `session_mode=0`, selecting "Session Mode" behaves exactly like old Infinite Mode (all-wreck reset loop, hot reload).
3. When `session_mode=1`:
   - Session starts by running one qualifying race per car type.
   - During a qualifying race, only cars of that type are on track.
   - After qualifying, the winner is moved to the back of that type's grid.
   - Series points are awarded after each qualifying race.
   - The main series begins with all types on their qualifying grid.
   - Main series races accrue points normally.
4. Series stats pane shows "Session: Qualifying (N/M) — Type: <type_name>" during qualifying, with the correct type name for each qualifying race.
5. Series stats pane shows "Session: Main Race N" during the main series.
6. Single-car types skip simulation and auto-complete qualifying in zero time.
7. `series_races` config controls main series race count (qualifying is additional).
8. Config keys `session_mode`, `qualifying_laps`, and `qualifying_races` are optional and default to safe values.

## Validation Checklist

- Set `session_mode=0`, load cars, select "Session Mode" — confirm it runs infinite loop (all-wreck reset loop).
- Set `session_mode=1`, load 2 cars of the same type + 2 of another type (4 cars total), select "Session Mode":
  - Confirm 2 qualifying races run (one per type).
  - During each qualifying race, only same-type cars are visible/active.
  - After each qualifying race, winner is moved to the back of that type's grid.
  - Points are awarded after each qualifying race.
  - Main series starts with all 4 cars on the qualifying-determined grid.
- Verify series stats pane shows correct session context line (including car type during qualifying).
- During each qualifying race, verify series stats pane shows the correct type name: e.g., "Session: Qualifying (1/2) — Type: car" then "Session: Qualifying (2/2) — Type: car2".
- Verify single-car type qualifying completes instantly with no simulation.
- Verify backward compatibility: loading existing config without `session_mode` key works and "Session Mode" runs infinite loop.
- Set `qualifying_races=2` with 2 types and confirm each type runs 2 qualifying races (4 total) before the main series begins, with winner-goes-to-back reorder after each.

## Risks and Notes

1. **Car-type grouping by source_file**: E.g., instances `CloverFPV` and `CloverFPV_2` both from `car.car` → both type "car". Instance `car2_2` from `car2.car` → type "car2". If different car models are needed as distinct types, they must come from different `.car` files.

2. **Grid reordering implementation**: After a qualifying race, reordering the grid for a specific type requires tracking which cars belong to that type, reordering their start poses, and applying the winner-to-back / rest-up transformation. This is a shallow operation — the `SimCar.start_pose` is updated, and the race session resets with the new poses.

3. **Single-car type optimization**: Skipping simulation for single-car types is a UI/UX optimization. The alternative (running a full race with one car) is harmless but wastes time. A flag like `skip_qualifying = len(type_cars) < 2` gates the optimization.

4. **Qualifying lap count**: Using the same `series_laps` value for qualifying may make qualifying races too long. The separate `qualifying_laps` config allows shorter qualifying sessions.

5. **Mixed types with different car counts**: If type A has 4 cars and type B has 2 cars, after qualifying, the main series grid will have A's 4 cars in their qualifying order followed by B's 2 cars in their qualifying order. The grid merge order is: type in load order, cars within type in qualifying order.

6. **Inter-type car visibility during qualifying**: During qualifying, only one type's cars are active. Other types' cars should be removed from physics simulation and not rendered (or rendered as ghost/transparent). Hiding them entirely during qualifying is cleanest.

7. **Series stats pane layout**: The session context line is added at the top of the series stats pane. If the pane is already dense, the font may need to shrink. The existing `draw_lines_fit_segmented` with variable sizing should handle this.
