# Specsheet: Tuning Car Behavior and Multi-Car Loading

## Overview

Define adaptive car-driving behavior focused on speed and race-time improvement, and extend track simulation to support loading, placing, and resetting multiple unique cars.

## Objective

Deliver a behavior model that encodes each car's priorities and learning memory, plus simulator workflows for multi-car placement and per-car stat inspection.

## Source Prompt

- cars want to hit their top speeds above all else
- cars want to go faster than their previous times
- cars do not want to take damage
- cars do not want to slow down
- cars want to keep their nose going forward
- cars are ok with losing control as long as it doesn't slow them down
- cars do not want to hit barriers
- cars remember their previous 10 races and learn from them
- cars track lap time and adjust driving strategy
- each car adapts its own strategy to maximize winning chances
- express behavior and learning as a data type (class or struct)
- simulator supports loading multiple cars
- load map, load car, place car, load another car, repeat
- saving map preserves car layout and track configuration
- tracks remember all starting positions
- reset map returns cars to original starting positions
- cars cannot be duplicated by identity; same base car can be loaded with numeric suffixes for uniqueness
- stats window requires dropdown to select which car to display

## Scope

### In scope

- Car behavior specification as explicit data structures
- Per-car memory of last 10 races
- Strategy adaptation inputs from lap time and incident history
- Multi-car loading and drag placement on track
- Persistence of all loaded cars and start poses with track save
- Reset action that restores each car to saved starting pose
- UI stats selector for active car in HUD

### Out of scope

- Full ML framework training pipeline
- Networked multiplayer or remote synchronization
- Visual design overhaul beyond required stats selector controls

## Behavior Model Requirements

### Driving priorities

The control model must optimize for:

1. Achieving and sustaining high speed near car max speed
2. Improving over previous lap/race times
3. Avoiding damage and barrier impacts
4. Preserving forward nose orientation along route direction
5. Avoiding unnecessary deceleration
6. Allowing controlled instability when it does not reduce progress

### Learning and memory constraints

1. Each car stores results from its last 10 completed races.
2. Each memory entry includes at minimum:
   - Total race time or lap time summary
   - Damage taken
   - Barrier contacts/crashes
   - Average speed
   - Completion status
3. Oldest records are discarded when exceeding 10 entries.
4. Adaptation is per-car and must not leak behavior data across cars.
5. Strategy updates use historical outcomes to bias future control targets.

### Required data types

Implement as explicit class/struct-style models, such as:

- CarBehaviorProfile
  - priority weights (speed, safety, smoothness, orientation)
  - aggressiveness/risk tolerance
  - target speed policy fields
- CarRaceMemory
  - fixed-size collection (max 10) of CarRaceOutcome
- CarRaceOutcome
  - timing, damage, collisions, completion flags, derived metrics
- CarLearningState
  - adjustment deltas derived from memory (for next race)

These can be integrated into existing car runtime/config models, but type boundaries must remain clear.

## Multi-Car Simulator Requirements

### Load and placement workflow

1. User can load a track.
2. User can load one car at a time repeatedly.
3. After each load, user can drag/place that car onto valid racing surface.
4. Workflow supports repeating load-and-place for multiple cars before race start.

### Uniqueness rules

1. Every loaded car instance must have a unique runtime identity.
2. If same car config is loaded multiple times, auto-rename with numeric suffix:
   - Example: car, car_2, car_3
3. Uniqueness must be stable in UI, state, and persistence.

### Persistence rules

1. Saving map/track must persist:
   - Track configuration
   - All loaded car references
   - Starting positions and headings for each car
2. Reset map/race must restore all cars to persisted start poses.
3. Reset must not delete loaded cars; only restore their starting state.

## UI Requirements

### Stats selector

1. Stats panel includes a dropdown to select which car's stats to show.
2. Dropdown lists all currently loaded unique car names.
3. Selected car drives displayed values (state, speed, tire, fuel, damage, laps, etc.).
4. If selected car is removed/unavailable, selection falls back to a valid remaining car.

## Functional Requirements

1. Introduce behavior and learning data types in shared model layer.
2. Track sim runtime supports multiple concurrent car runtime states.
3. Existing single-car flows remain functional when only one car is loaded.
4. Multi-car load/placement works through both menu and keyboard paths where applicable.
5. Save/load round-trip preserves multi-car start grid layout.
6. Reset operation restores all cars to original saved start poses.
7. Per-car memory updates after race completion and is capped at 10 entries.
8. Strategy adaptation uses memory values to modify control decisions in subsequent races.

## Non-Functional Requirements

- Deterministic behavior updates for reproducible debugging
- Backward compatibility for old car/track files where possible
- No regressions to menu layering and interaction reliability
- Maintain responsive simulation loop with practical frame stability

## Acceptance Criteria

1. User can load three cars, place them at different valid positions, and start race.
2. Loading same base car multiple times creates unique suffixed names automatically.
3. Saving then reloading map restores all loaded cars and their start positions.
4. Reset returns all cars to saved initial positions and headings.
5. Stats dropdown switches displayed data between loaded cars correctly.
6. Each car stores no more than 10 race memories.
7. Behavior parameters change between races based on stored outcomes.
8. Single-car mode continues to work without requiring multi-car setup.

## Validation Checklist

- Verify duplicate car load names are uniquified and stable.
- Verify drag placement restrictions keep cars on racing surface.
- Verify save file contains all car start poses and identifiers.
- Verify reset restores every car pose exactly.
- Verify stats dropdown reflects all loaded cars and updates correctly.
- Verify race-memory cap trimming at 10 entries.
- Verify adaptive parameters differ after at least two completed races.

## Risks and Notes

- Multi-car collision and autonomy interactions can increase instability; isolate behavior logic per car.
- Aggressive speed optimization may conflict with safety constraints; tune with weighted priorities.
- Persistence schema changes may require migration handling for older track files.
