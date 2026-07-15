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
- Arrow keys: drive selected car (manual override)
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
- `trackgen.conf`
  - `window_width`, `window_height`: generator window size
  - `tracks_dir`: relative output/input directory for tracks
  - `lane_width`: default generated track lane width (higher value = wider track)

If `default_track` is set but not found, TrackSim starts and shows a status message describing the missing file.

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
