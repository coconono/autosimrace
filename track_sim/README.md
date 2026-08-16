# track_sim

Pygame racing simulation suite with three runnable programs:

- Car editor
- Track generator
- Track simulation (multi-car race sandbox)

## Prerequisites

- Python 3.9+ with `venv` support
- macOS or Linux shell (`bash`)

## Program Launch

From repo root, run one of:

```bash
./tools/track_sim/bin/run_careditor.sh
./tools/track_sim/bin/run_trackgen.sh
./tools/track_sim/bin/run_tracksim.sh
```

Each launcher will:

1. Create `.venv` in `tools/track_sim` if missing.
2. Activate the environment.
3. Install dependencies from `requirements.txt`.
4. Run the selected program.
5. Deactivate the environment on exit.

Legacy compatibility launcher:

```bash
./tools/track_sim/bin/run.sh
```

This now starts the track simulation.

## What Is Implemented

- Car editor with visual body/nose color controls and car file save/load
- Procedural track generation and track file save/load
- Multi-car track simulation with autonomous driving and optional manual override for selected car
- Car placement and overlap resolution when loading and dragging
- Start grid drag/reposition in track generator (constrained to racing surface)
- Drag-placed cars auto-align to local track direction in track simulation
- Lap counting, lap timing, race stats, and per-car debug panel
- Race stats `Laps Completed` shows race leader laps (max laps), not sum across all cars
- Tires are fully regenerated for a car when it completes a lap
- Off-track penalties, tire/fuel/damage model, and crash state
- Per-race decision logs with reasons (braking/coasting/left-track/crashed)

## Controls

### Car Editor

- Up/Down: select a field
- Left/Right: adjust selected field
- N: edit car name
- S: save to `cars/*.car`
- L: load first saved car
- Q: quit

### Track Generator

- G: generate a track
- R: reset current track
- D: discard and regenerate
- N: rename track
- S: save to `tracks/*.track`
- L: load latest track
- Mouse: drag the yellow start grid to reposition it on the racing surface
- Q: quit

### Track Simulation

- L: load track
- C: load car
- N: start/reset race
- A: toggle autonomous/manual mode
- H: toggle stats panel
- Mouse: select and drag cars when race is not running
- Dragged cars automatically rotate to match local track direction
- Q: quit

Top-bar menus provide the same key actions plus save/reset/pause options.

## Configuration

Configuration files live in `tools/track_sim/etc/`:

- `tracksim.conf`
  - `window_width`, `window_height`: simulation window size
  - `tracks_dir`, `cars_dir`: relative directories for track/car files
  - `default_track`: track file to auto-load on startup (example: `cocorp.track`)
  - `training_races`: number of races to run in Simulate (training) mode
  - `series_name`: display name for a race series (empty disables series UI)
  - `series_races`: number of races in the series (0 = use `training_races` / infinite mode)
  - `series_logo`: path to series logo image (relative to `images/logos/` or absolute path)
  - `capture_width`, `capture_height`: streaming frame size (single source of truth for the `ASR_STREAM=1` build — must match what `asr-stream-run` reads)
  - `stream_show_panes`: when `1` AND `ASR_STREAM=1`, the stream shows a leaderboard + bottom-stats overlay instead of a pure fullscreen track. The overlay is rendered by a **separate process** (`src.common.asr_stream_hud`) so that work (and the full-frame composition) runs on its own core; the renderer ships only the track-region pixels and publishes a HUD snapshot over a datagram socket when the standings change. Default `0`.
  - `stream_leaderboard_width`, `stream_bottom_stats_height`: sizes of the two HUD panes (right leaderboard column width, and bottom-stats strip height) used when `stream_show_panes=1`. The race (track) pane takes the remaining space, so **making either value smaller enlarges the on-track view**. Defaults `280` / `220`.
- `trackgen.conf`
  - `window_width`, `window_height`: generator window size
  - `tracks_dir`: relative output/input directory for tracks
  - `lane_width`: default generated track lane width (higher value = wider track)

If `default_track` is set but not found, TrackSim starts and shows a status message describing the missing file.

## Race Series

A race series is a group of races where points accumulate across all races. Points are awarded per race:

- 1st place: 5 points
- 2nd place: 4 points
- 3rd place: 3 points
- 4th place: 2 points
- Completing the race: 1 point

The series winner is the car with the most points after the configured number of races. Series stats (fastest/slowest lap, points leaders) are tracked across all races in the series.

### Series Logo

The series logo is displayed in the series stats pane. Place logo images in `tools/track_sim/images/logos/` and reference them via the `series_logo` config key.

Logo size requirements:

- Recommended dimensions: 200x200 pixels (square)
- Maximum dimensions: 240x240 pixels
- Supported formats: PNG (with alpha), JPG, BMP
- The logo is scaled to fit within the series stats pane while preserving aspect ratio

## Race Logs

Race logs are written to `tools/track_sim/logs/` with file names:

- `race-YYYYMMDD-HHMMSS-TRACKNAME.log`

Each log includes entries like:

- `mode=braking` with reason (for example: `overspeed`, `turn_guard`, `traffic_close`, `stopped_hazard_near`, `emergency_hazard`)
- `mode=coasting` with reason (`turn_speed_match`, `straight_speed_match`)
- `mode=left_track` with reason `car_footprint_off_surface`
- `mode=crashed` with reason (`damage_limit`, `fuel_depleted`, `damage_and_fuel_depleted`)

Log retention is capped at 10 race logs. Oldest logs are pruned first when a new race log is created.

## Project Structure

- `bin/`: launcher scripts
- `etc/`: program configuration files
- `src/common/`: shared models, IO, geometry, and physics
- `src/careditor/`: car editor program
- `src/trackgen/`: track generator program
- `src/tracksim/`: track simulation program
- `cars/`: saved car configs
- `tracks/`: saved track layouts
- `logs/`: per-race decision logs

## Notes

- Window size is configured to 1600x900 for all programs.
- Track files use `.track` JSON format.
- Car files use `.car` JSON format.
- Race log files are ignored by git via root `.gitignore`.
- In TrackSim, the debug pane is hidden by default and can be toggled from the Stats menu.

## Troubleshooting

- If virtual environment creation fails, install Python with `venv` support.
- If pygame import fails, rerun one of the launch scripts to reinstall dependencies.
- If no track loads in simulation, generate and save one with track generator first.
