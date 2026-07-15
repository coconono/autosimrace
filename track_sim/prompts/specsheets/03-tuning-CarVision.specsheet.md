# Specsheet: Car Vision and Waypoint Navigation

## Overview

Replace centerline-only autonomous driving with a waypoint-plus-vision system that can reason about obstacles, adapt route choices, and render car perception/route artifacts in the simulator UI.

## Objective

Implement deterministic per-car waypoint and vision planning so each car can:

1. Follow efficient reusable permanent waypoints.
2. React to dynamic obstacles using a vision matrix.
3. Lay and evaluate temporary waypoints.
4. Improve route quality over laps without violating track boundaries.

## Source Prompt

- Use waypoints as the core route representation.
- Use a 70 degree forward vision cone at 5x car length.
- Maintain a 3x3 vision matrix (left/center/right x near/mid/far) with states: clear, waypoint, wreck, car, barrier.
- Prioritize: accelerate waypoint-to-waypoint, avoid obstacles, prefer turning > coasting > drifting > braking > reversing.
- Create temporary waypoints for blocked/unknown paths or damage escape.
- Promote temporary waypoint to permanent only if lap improves best lap time.
- Show selected-car vision cone, permanent waypoints, and temporary waypoint markers in tracksim.
- Add stats-menu visibility toggles and race stats pane with best/worst lap holders.

## Review Findings (Current Implementation Gaps)

### High Severity

1. No waypoint model or persistence exists.

- There is no waypoint data in runtime car model structures in [src/common/models.py](src/common/models.py).
- Driving remains centerline-index based in [src/tracksim/main.py](src/tracksim/main.py#L415) and [src/tracksim/main.py](src/tracksim/main.py#L472).

1. No vision cone or matrix exists.

- No vision or matrix structures/logic found in [src/tracksim/main.py](src/tracksim/main.py) or [src/common/models.py](src/common/models.py).

1. Obstacle handling is geometric/reactive, not matrix-driven.

- Obstacle inputs are raw traffic tuples in [src/tracksim/main.py](src/tracksim/main.py#L593), not classified matrix cells.

### Medium Severity

1. Prompt-specified panel/menu UX is not implemented as requested.

- There is no dedicated stats menu with per-pane visibility toggles in [src/tracksim/main.py](src/tracksim/main.py#L1023).
- Race stats pane with best/worst lap-holder summary is missing (current stats/debug only).

1. No temporary waypoint lifecycle (create/promote/discard) exists.

- No temporary/permanent waypoint promotion logic tied to lap improvement is present.

### Low Severity

1. Existing learning/logging can be reused but do not satisfy vision prompt semantics.

- Existing decision logs and lap learning are valuable, but not tied to waypoint/vision model.

## Scope

### In scope

- Waypoint graph model (permanent + temporary) per car.
- Waypoint generation on track load and per-car reuse persistence.
- Vision cone sampling and 3x3 matrix state classification.
- Matrix-driven steering/throttle/brake state machine.
- Temporary waypoint creation and promotion/discard policy.
- Selected-car debug render of cone and waypoint markers.
- Race stats pane and stats-menu visibility toggles.

### Out of scope

- ML/NN training systems.
- Network synchronization.
- Full replay system.

## Data Model Requirements

Add explicit types in [src/common/models.py](src/common/models.py):

1. `VisionCellState` enum/string literals:

- `clear`, `waypoint`, `wreck`, `car`, `barrier`

1. `VisionMatrix`:

- 3x3 cells indexed by x (`left`,`center`,`right`) and y (`near`,`middle`,`far`)
- Each cell stores dominant state and optional confidence/priority

1. `Waypoint`:

- `x`, `y`, `kind` (`permanent` or `temporary`), `source` (`generated`, `damage_escape`, `blocked_path`, `no_waypoint_visible`)
- `created_lap`, `evaluation_pending`, optional `candidate_id`

1. `CarRoutePlan` (per car):

- ordered permanent waypoints (loop)
- temporary waypoint queue/list
- active target waypoint index
- last known bearing to next permanent waypoint

1. Extend `SimCar` runtime in [src/tracksim/main.py](src/tracksim/main.py):

- route plan
- latest vision matrix
- temporary-waypoint evaluation context

## Persistence Requirements

1. Track metadata stores per-car permanent waypoints keyed by unique `instance_name`.
2. Temporary waypoints are runtime-only unless promoted.
3. Save/load round-trip restores permanent waypoints and active route order.
4. Compatibility: if waypoint metadata missing, generate defaults on load.

## Waypoint Generation Requirements

On load/start:

1. Generate an efficient loop on racing surface with minimal waypoint count.
2. Segment-to-segment lines must remain on racing surface boundary constraints.
3. Waypoint spacing adapts to curvature:

- fewer on straights
- more near corners

4. Route direction must align with racing direction (`nav_direction`).

## Vision System Requirements

1. Vision cone geometry:

- angle: 70 degrees centered on heading
- range: `5.0 * car.length`

1. Matrix construction each frame:

- divide cone into x sectors (`left`,`center`,`right`) and y bands (`near`,`middle`,`far`)
- classify dominant state per cell by nearest/highest-priority hit
- priority order for mixed occupancy: `barrier > wreck > car > waypoint > clear`

1. Inputs to classification:

- barriers/track boundaries
- wrecked cars
- moving/stopped cars
- current target waypoint and nearby route waypoints

## Control Logic Requirements

### Priority stack

Implement in order:

1. Drive toward target waypoint.
2. Avoid obstacle states from vision matrix.
3. Action preference ordering when multiple options valid:

- turning > coasting > drifting > braking > reversing

### Temporary waypoint rules

Create temporary waypoint when:

1. Damage spike detected (escape forward-away vector).
2. Next waypoint not visible in matrix.
3. Path to next waypoint blocked by obstacle state.

Temporary waypoint placement must:

1. Stay on racing surface.
2. Bias toward last known bearing to permanent waypoint.
3. Avoid immediate obstacle sectors.

Promotion/discard:

1. Tag temporary waypoint with lap created.
2. When lap completes, if lap beats current best lap by threshold, promote relevant temporary waypoint(s) to permanent.
3. Otherwise discard temporary candidate(s).

## UI Requirements

### Selected-car overlays

When a car is selected in tracksim:

1. Draw vision cone wedge.
2. Draw permanent waypoints as solid circles/linked path.
3. Draw temporary waypoints as dotted-outline circles/segments.

### Stats controls and race stats pane

1. Add `Stats` menu dropdown with visibility toggles for:

- car stats pane
- debug pane
- race stats pane

1. Add race stats pane with:

- current laps completed
- best lap time and car name
- worst lap time and car name

## Functional Requirements

1. Existing autonomous mode remains available.
2. New waypoint/vision mode can fully drive a race with multiple cars.
3. Existing collision, lap counting, and logging continue to work.
4. Logging should include waypoint/vision decision context (optional extension in same log file format).

## Non-Functional Requirements

1. Deterministic updates given same seed/state.
2. Maintain frame responsiveness at current scale.
3. Backward-compatible load for old tracks/cars.
4. Keep code paths modular (generation, matrix build, control policy, rendering).

## Acceptance Criteria

1. On race start, each car has a valid permanent waypoint loop entirely on racing surface.
2. Selected car shows cone + permanent + temporary waypoint overlays.
3. Vision matrix updates each frame and reflects nearby cars/wrecks/barriers/waypoints.
4. Cars create temporary waypoints when blocked, damaged, or waypoint not visible.
5. Temporary waypoints are promoted only on lap improvement; otherwise discarded.
6. Permanent waypoints persist in saved track metadata and reload correctly.
7. Race stats pane shows best and worst lap holder info.
8. Stats menu visibility toggles work for all panes.

## Validation Checklist

- Verify waypoint segment surface validity for generated loops.
- Verify matrix sector/band classification by rendering debug text for selected car.
- Verify blocked-path scenario triggers temporary waypoint generation.
- Verify promotion/discard behavior across at least two laps.
- Verify save/load round-trip for per-car permanent waypoints.
- Verify race stats pane values update and match recorded lap data.

## Risks and Notes

1. Over-dense waypoint generation can destabilize steering oscillations.
2. Matrix aliasing near sector boundaries can cause control jitter; include smoothing/hysteresis.
3. Promotion policy can overfit to noisy laps; apply minimum-improvement threshold.
4. Multi-car uniqueness keys must remain stable for waypoint persistence mapping.
