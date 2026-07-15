# Specsheet: Initial makeTrack Implementation

## Overview

Build the first functional version of the racing simulation suite with focus on:

- Track generation
- Single-car simulation on generated/loaded tracks
- Shared project structure and reusable modules for future multi-car expansion

## Objective

Deliver three pygame programs with consistent UX and shared core libraries:

1. Car editor
2. Track generator
3. Track simulation

This phase prioritizes correctness and extensibility over visual polish.

## Source Prompt Summary

- Track is a closed loop with outer and inner barriers built from fixed pieces.
- Car is a physical rectangle with state, motion, and health resources.
- First pass supports one car; architecture must scale to multi-car.
- Unified 1600x900 pygame window and top dropdown menu pattern.
- Shared directories, configs, run scripts, and reusable modules.

## Scope

### In scope (first pass)

- Core data models for barrier pieces, track layout, and car state
- Track generation for valid loops using fixed piece templates
- Track save/load to `.track` files
- Single-car simulation loop with movement, collisions, drift condition, and lap counting
- Car editor for creating and saving car configurations
- Shared menu bar with Start menu (`save`, `load`, `quit`)
- Program-specific run scripts in `bin/`
- Program config files in `etc/`
- Asset/template usage from `images/`

### Out of scope (first pass)

- Multi-car race logic and interactions beyond architecture placeholders
- Advanced AI/ML behavior networks
- High-fidelity physics tuning
- Production packaging/deployment

## Definitions and Rules

### Track model

- Track consists of two barrier loops:
  - Outer barrier loop
  - Inner barrier loop
- Barrier pieces are fixed-size objects and cannot be stretched/compressed.
- Piece types include curve, short straight, and long straight.
- All piece rotations are multiples of 90 degrees.
- Every barrier piece connects to exactly two pieces of the same barrier type.
- Barrier pieces must not overlap.
- Racing surface is the area between inner and outer loops.
- Starting grid is a racing-surface area adjacent to one short inner barrier.

### Lap rule

- A lap counts only when the car re-enters the starting grid after first leaving it.
- Direction requirement: counter-clockwise traversal.

### Car model

- Car is a rectangle with length, width, position, and heading.
- Required properties:
  - Name
  - Mass
  - Max speed
  - Tire health
  - Fuel
  - Damage
  - Current speed
  - Current direction
- States:
  - `stopped`
  - `braking`
  - `moving_forward`
  - `turning_left`
  - `turning_right`
  - `reversing`
  - `drifting`
  - `crashed`

## Target Project Structure

Under `tools/track_sim`:

- `bin/`
  - `run_careditor.sh`
  - `run_trackgen.sh`
  - `run_tracksim.sh`
- `etc/`
  - `trackgen.conf`
  - `tracksim.conf`
  - `careditor.conf` (optional but recommended for consistency)
- `src/`
  - `common/` shared models/utilities
  - `careditor/`
  - `trackgen/`
  - `tracksim/`
- `tracks/` track layout files (`.track`)
- `cars/` car config files (`.car`)
- `images/` track shape templates and UI assets
- `README.md`
- `requirements.txt`

## Architecture Requirements

1. Reuse-first design: shared logic lives in `src/common/`.
2. Program entry points are isolated per program under `src/<program>/`.
3. Config-driven behavior for paths, defaults, and window size.
4. Deterministic serialization formats for car and track files.
5. Explicit model boundaries:
   - Geometry/track validation
   - Physics/state update
   - Rendering/UI
   - Persistence

## Program Specifications

### 1) Car Editor

#### Functional requirements

- Open 1600x900 window with unified top dropdown menu bar.
- Provide car-name dropdown with editable car fields:
  - Name
  - Front/back orientation metadata
  - Mass
  - Maximum speed
  - Starting tire health
  - Starting fuel
- Validate value ranges before save.
- Save/load car configs to/from `cars/`.

#### Acceptance criteria

1. User can create a car config and save it.
2. Saved config can be loaded and displayed accurately.
3. Invalid input is blocked with clear feedback.

### 2) Track Generator

#### Functional requirements(Track generator)

- Open 1600x900 window and initialize menus:
  - Start: `load`, `save`, `quit`
  - Generate: `generate`, `reset`
  - Validate: `name`, `save`, `quit`, `discard`
- Generation pipeline:
  1. Build outer loop from 8 random outer pieces.
  2. Validate continuous non-overlapping loop with degree-2 connectivity.
  3. Build matching inner loop with proper spacing for side-by-side cars.
  4. Select starting grid adjacent to a short inner barrier.
  5. Render proposed layout.
  6. Repeat generation until valid layout is produced.
- Save validated tracks as `.track` files in `tracks/`.
- Discard command restarts generation.

#### Acceptance criteria(Track generator)

1. Generator can produce and display at least one valid closed loop.
2. Invalid layouts are rejected automatically.
3. Saved `.track` file reloads without structural loss.
4. Start grid is present and persisted.

### 3) Track Simulation

#### Functional requirements(Track simulation)

- Open 1600x900 window and show:
  - Track rendering
  - Car status panel (state, speed, tire health, fuel, damage)
  - Top menu (`new race`, `load track`, `quit`)
- New race flow:
  - Require track loaded before race starts.
  - Initialize car at valid start position.
  - Reset tire health, fuel, and damage.
- Racing loop:
  - Update position/orientation from velocity, acceleration, steering.
  - Handle collisions with barriers.
  - Apply friction and drift condition when grip is low.
  - Transition to `crashed` when critical conditions are met.
  - Count laps with starting-grid leave/re-enter rule.

#### Acceptance criteria(Track simulation)

1. User can load a `.track` file and begin a race.
2. Car movement updates in real time with visible status changes.
3. Collisions and crashed state are visibly reflected.
4. Lap count increments only for valid full-circuit completion.

## File Format Requirements

### `.track` file

Must store enough data to reconstruct:

- Piece list for outer and inner barriers
- For each piece: type, position, orientation, and connected piece IDs
- Start grid geometry/identifier
- Optional metadata: name, seed, created timestamp

### Car config file

Must store at minimum:

- Name
- Geometry (length/width)
- Mass
- Max speed
- Starting tire health
- Starting fuel

## Run Script Requirements

Each program script in `bin/` must:

1. Ensure `.venv` exists under `tools/track_sim`.
2. Activate `.venv`.
3. Install/update dependencies from `requirements.txt`.
4. Execute program entry script.
5. Deactivate `.venv` on exit.
6. Return non-zero on failure.

Required scripts:

- `run_careditor.sh`
- `run_trackgen.sh`
- `run_tracksim.sh`

## UI and Interaction Standards

- Window size fixed at 1600x900.
- Shared dropdown menu bar in all programs.
- Start menu leftmost and consistent with `save`, `load`, `quit`.
- Program actions should be keyboard/mouse operable.
- Clear user feedback for errors and invalid operations.

## Non-Functional Requirements

- Code must be modular and documented.
- Common libraries must be reusable across all programs.
- Naming and directory conventions must remain consistent.
- First-pass performance target: stable 60 FPS on single-car simulation where practical.

## Implementation Phasing

### Phase 1 (must deliver)

- Shared core models and geometry/validation primitives
- Track generator (valid loop generation + save/load)
- Track simulation with one car and lap logic
- Basic car editor and persistence

### Phase 2 (deferred)

- Multi-car support and proximity interactions
- Expanded behavior network
- Enhanced crash VFX logic for grouped crashes

## Validation Checklist

- `bin/run_trackgen.sh` launches and can generate/save/load tracks.
- `bin/run_tracksim.sh` launches, loads tracks, and runs one-car simulation.
- `bin/run_careditor.sh` launches and saves/loads car configs.
- All programs use 1600x900 and shared top menu bar pattern.
- `.track` data round-trips without connectivity corruption.
- Lap counting respects leave-then-reenter rule at start grid.
- Repository has updated `README.md` and `requirements.txt`.

## Risks and Notes

- Geometry validity (non-overlap and closed-loop constraints) is the highest technical risk.
- Physics realism should remain simplified and deterministic in first pass.
- Prompt includes multi-car crash VFX notes; keep interfaces extensible but do not block first-pass single-car delivery on these effects.
