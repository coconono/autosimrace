# Specsheet: Line-Following First, Waypoint-Secondary Control

## Overview

Refine autonomous control to prioritize continuous line-following behavior over discrete waypoint chasing, reducing overshoot stalls and improving race-like flow.

## Objective

Implement line-following-first control semantics where cars:

1. Track near/mid/far line guidance continuously.
2. Use waypoints as continuity anchors, not as hard stop targets.
3. Learn/maintain car-specific lateral line preference across laps.
4. Freeze line adaptation for current race leader.

## Source Prompt (Revised)

- Cars are line-following first, not waypoint-chasing.
- Waypoints can remain for continuity but should not cause stop-and-go behavior.
- Cars should maintain near/mid/far line awareness.
- Cars can adapt a lateral line preference over laps.
- Lateral line preference magnitude must be tunable in Car Editor.
- Current race leader should not continue line-offset adaptation.

## Prompt Review Findings

### Conflicts in original prompt

1. "Waypoints are not used for navigation" conflicts with "adjust waypoints laterally".
2. "Follow line" was underspecified (no clear control precedence or fallback behavior).

### Resolution

1. Define waypoint role as secondary continuity anchor only.
2. Define line-following as the primary steering/throttle objective.
3. Define adaptation/freeze behavior based on leader status.

## Scope

### In scope

1. Control-policy precedence updates for line-following-first behavior.
2. Lateral line preference state per car.
3. Car Editor tunable for line-offset scale.
4. Leader-aware adaptation freeze.
5. Telemetry/logging reason updates for line-following decisions.

### Out of scope

1. New physics model.
2. Full route model replacement.
3. ML/NN systems.

## Functional Requirements

1. Primary objective is minimizing line-tracking error over near/mid/far horizon.
2. Waypoint error must not trigger abrupt braking when line tracking is healthy.
3. If waypoint and line objectives disagree, line objective wins unless safety constraints are violated.
4. Each car stores a runtime preferred lateral offset from centerline (signed offset along track normal).
5. Preferred lateral offset can be adjusted after lap completion based on stability/performance heuristics.
6. If car is race leader, lateral offset adaptation is paused (offset retained, not updated).
7. Existing lap counting, race completion, and crash logic must remain intact.

## Data and State Requirements

In TrackSim runtime state (SimCar and/or learning state):

1. Add preferred line offset per car.
2. Add optional adaptation accumulator/history fields if needed for smoothing.
3. Track whether adaptation is currently frozen due to leader status.

## Car Editor Requirements

1. Add tunable parameter for line-offset adaptation scale (for example: line_offset_scale).
2. Save/load this parameter in car files.
3. Keep defaults safe and backward-compatible for older car files.

## Control Logic Requirements

1. Build/retain a local track tangent and normal at the car position.
2. Define target line point using centerline plus preferred lateral offset along normal.
3. Steering and speed planning should prioritize this target line over direct waypoint point-lock.
4. Keep existing hazard and off-track safety constraints as hard overrides.
5. Maintain smoothness: avoid oscillatory left-right over-correction due to offset changes.

## Leader Freeze Requirements

1. Determine race leader using current race stats logic (laps, then progress tie-breaker).
2. For leader car only, stop updating preferred line offset adaptation.
3. Non-leaders continue adapting.
4. If leader changes, freeze follows the new leader dynamically.

## UI/Telemetry Requirements

1. Optional debug text for selected car should include:
   - preferred line offset
   - adaptation frozen/unfrozen status
2. Race behavior logs should include reason codes indicating line-follow preference and waypoint-deprioritization events where relevant.

## Acceptance Criteria

1. Cars no longer brake hard solely due to minor waypoint overshoot when line tracking is valid.
2. On long straights and sweeps, cars maintain smooth continuous motion.
3. Car-specific line preference persists through laps within a race.
4. Leader car line preference stops adapting while leading.
5. Car Editor exposes and persists line-offset scale tuning.
6. Multi-car race remains stable with existing crash/lap systems.

## Validation Checklist

1. Compare before/after logs for reduced waypoint-overshoot stop events.
2. Run multi-lap race and confirm non-leaders adapt while leader is frozen.
3. Force leader swaps and verify freeze follows the active leader.
4. Verify old car files still load and use default tuning values.
5. Confirm no regression in lap counting, crash transitions, and race stats updates.

## Risks and Notes

1. Over-aggressive offset adaptation may increase wall contacts on narrow tracks.
2. Too little waypoint influence can hurt continuity in ambiguous sections; retain minimal continuity guard.
3. Leader freeze may lock in suboptimal line if leader is traffic-constrained; monitor race variance.
