# Prompt: Tune Line Following (De-Emphasize Waypoint Chasing)

## Problem

Current behavior can over-focus on discrete waypoint hit logic, causing overshoot and abrupt stopping. This is not race-like.

## Goal

Cars should drive by following the track line continuously, not by hard-braking to precise waypoint coordinates.

## Revised Requirements

1. Primary navigation uses line following.
2. The controller should keep the line aligned through near, mid, and far vision guidance.
3. Waypoints may remain as route references/checkpoints but must not dominate control decisions in a way that causes stop-and-go waypoint chasing.
4. Each car can maintain its own preferred lateral line offset after completing laps (driver style).
5. Lateral line offset magnitude must be tunable in Car Editor.
6. If a car is current race leader, freeze its line-offset adaptation (leader should defend current pace/line instead of continuing to drift line preference).

## Clarifications

1. "Not waypoint following" means waypoint hits are not the primary steering/throttle objective.
2. Waypoints can still be used for lap/routing continuity and safety fallbacks.
3. Lateral adjustments are defined relative to local track tangent/normal:
	- if tangent is vertical, lateral offset is horizontal
	- if tangent is horizontal, lateral offset is vertical

## Desired Outcome

Smoother, race-like cornering and straights with fewer overshoot stalls, while preserving lap progression and multi-car race stability.