from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CAR_STATES = (
    "stopped",
    "coasting",
    "braking",
    "moving_forward",
    "turning_left",
    "turning_right",
    "reversing",
    "drifting",
    "crashed",
)

VISION_X_BINS = ("left", "center", "right")
VISION_Y_BINS = ("near", "middle", "far")
VISION_CELL_STATES = ("clear", "waypoint", "wreck", "car", "barrier")
WAYPOINT_KINDS = ("permanent",)
WAYPOINT_SOURCES = ("generated",)


@dataclass
class VisionCell:
    state: str = "clear"
    distance: float = 0.0


@dataclass
class VisionMatrix:
    cells: dict[str, VisionCell] = field(default_factory=dict)

    @staticmethod
    def _key(x_bin: str, y_bin: str) -> str:
        return f"{x_bin}:{y_bin}"

    @classmethod
    def empty(cls) -> "VisionMatrix":
        cells = {
            cls._key(x_bin, y_bin): VisionCell()
            for y_bin in VISION_Y_BINS
            for x_bin in VISION_X_BINS
        }
        return cls(cells=cells)

    def get(self, x_bin: str, y_bin: str) -> VisionCell:
        return self.cells.get(self._key(x_bin, y_bin), VisionCell())

    def set(self, x_bin: str, y_bin: str, state: str, distance: float) -> None:
        self.cells[self._key(x_bin, y_bin)] = VisionCell(state=state, distance=distance)


@dataclass
class Waypoint:
    x: float
    y: float
    kind: str = "permanent"
    source: str = "generated"
    created_lap: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "kind": self.kind,
            "source": self.source,
            "created_lap": self.created_lap,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Waypoint":
        raw_kind = str(data.get("kind", "permanent"))
        raw_source = str(data.get("source", "generated"))
        kind = raw_kind if raw_kind in WAYPOINT_KINDS else "permanent"
        source = raw_source if raw_source in WAYPOINT_SOURCES else "generated"
        return cls(
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            kind=kind,
            source=source,
            created_lap=int(data.get("created_lap", 0)),
        )


@dataclass
class CarRoutePlan:
    permanent_waypoints: list[Waypoint] = field(default_factory=list)
    active_target_index: int = 0
    last_known_bearing: float = 0.0

    def active_waypoint(self) -> Waypoint | None:
        if not self.permanent_waypoints:
            return None
        return self.permanent_waypoints[self.active_target_index % len(self.permanent_waypoints)]

    def next_permanent_waypoint(self) -> Waypoint | None:
        if not self.permanent_waypoints:
            return None
        idx = (self.active_target_index + 1) % len(self.permanent_waypoints)
        return self.permanent_waypoints[idx]

    def advance_if_reached(self, x: float, y: float, threshold: float, allow_wrap: bool = True) -> bool:
        target = self.active_waypoint()
        if target is None:
            return False
        if (target.x - x) ** 2 + (target.y - y) ** 2 > threshold * threshold:
            return False
        if self.permanent_waypoints:
            if len(self.permanent_waypoints) == 1:
                return True
            at_last = self.active_target_index >= len(self.permanent_waypoints) - 1
            if at_last and not allow_wrap:
                return False
            self.active_target_index = (self.active_target_index + 1) % len(self.permanent_waypoints)
        return True


@dataclass
class BarrierPiece:
    piece_id: str
    piece_type: str
    position: tuple[float, float]
    orientation: int
    connects_to: tuple[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "piece_id": self.piece_id,
            "piece_type": self.piece_type,
            "position": [self.position[0], self.position[1]],
            "orientation": self.orientation,
            "connects_to": [self.connects_to[0], self.connects_to[1]],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BarrierPiece":
        return cls(
            piece_id=str(data["piece_id"]),
            piece_type=str(data["piece_type"]),
            position=(float(data["position"][0]), float(data["position"][1])),
            orientation=int(data["orientation"]),
            connects_to=(str(data["connects_to"][0]), str(data["connects_to"][1])),
        )


@dataclass
class TrackLayout:
    name: str
    outer_pieces: list[BarrierPiece]
    inner_pieces: list[BarrierPiece]
    outer_points: list[tuple[float, float]]
    inner_points: list[tuple[float, float]]
    start_grid: tuple[float, float, float, float]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outer_pieces": [piece.to_dict() for piece in self.outer_pieces],
            "inner_pieces": [piece.to_dict() for piece in self.inner_pieces],
            "outer_points": [[x, y] for x, y in self.outer_points],
            "inner_points": [[x, y] for x, y in self.inner_points],
            "start_grid": [
                self.start_grid[0],
                self.start_grid[1],
                self.start_grid[2],
                self.start_grid[3],
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackLayout":
        return cls(
            name=str(data["name"]),
            outer_pieces=[BarrierPiece.from_dict(item) for item in data["outer_pieces"]],
            inner_pieces=[BarrierPiece.from_dict(item) for item in data["inner_pieces"]],
            outer_points=[(float(x), float(y)) for x, y in data["outer_points"]],
            inner_points=[(float(x), float(y)) for x, y in data["inner_points"]],
            start_grid=(
                float(data["start_grid"][0]),
                float(data["start_grid"][1]),
                float(data["start_grid"][2]),
                float(data["start_grid"][3]),
            ),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class CarConfig:
    name: str = "default_car"
    length: float = 56.0
    width: float = 28.0
    mass: float = 1100.0
    max_speed: float = 420.0
    line_offset_scale: float = 0.35
    starting_tire_health: float = 100.0
    starting_fuel: float = 100.0
    body_color: tuple[int, int, int] = (210, 42, 42)
    nose_color: tuple[int, int, int] = (245, 120, 120)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "length": self.length,
            "width": self.width,
            "mass": self.mass,
            "max_speed": self.max_speed,
            "line_offset_scale": self.line_offset_scale,
            "starting_tire_health": self.starting_tire_health,
            "starting_fuel": self.starting_fuel,
            "body_color": [self.body_color[0], self.body_color[1], self.body_color[2]],
            "nose_color": [self.nose_color[0], self.nose_color[1], self.nose_color[2]],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CarConfig":
        def _parse_color(key: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
            raw = data.get(key, default)
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                try:
                    r = max(0, min(255, int(raw[0])))
                    g = max(0, min(255, int(raw[1])))
                    b = max(0, min(255, int(raw[2])))
                    return (r, g, b)
                except (TypeError, ValueError):
                    return default
            return default

        return cls(
            name=str(data.get("name", "default_car")),
            length=float(data.get("length", 56.0)),
            width=float(data.get("width", 28.0)),
            mass=float(data.get("mass", 1100.0)),
            max_speed=float(data.get("max_speed", 420.0)),
            line_offset_scale=max(0.0, min(1.5, float(data.get("line_offset_scale", 0.35)))),
            starting_tire_health=float(data.get("starting_tire_health", 100.0)),
            starting_fuel=float(data.get("starting_fuel", 100.0)),
            body_color=_parse_color("body_color", (210, 42, 42)),
            nose_color=_parse_color("nose_color", (245, 120, 120)),
        )


@dataclass
class CarRuntimeState:
    x: float
    y: float
    heading_radians: float
    speed: float
    tire_health: float
    fuel: float
    damage: float
    vx: float = 0.0
    vy: float = 0.0
    yaw_rate: float = 0.0
    state: str = "stopped"
    laps: int = 0
    left_start_zone: bool = False
    cumulative_angle: float = 0.0
    nav_direction: int = 0
    nav_last_index: int = -1
    nav_stall_frames: int = 0
    wall_contact_frames: int = 0
    distance_traveled: float = 0.0
    last_lap_distance: float = 0.0
    last_x: float = 0.0
    last_y: float = 0.0

    def as_status_lines(self) -> list[str]:
        return [
            f"State: {self.state}",
            f"Speed: {self.speed:.1f}",
            f"Tire: {self.tire_health:.1f}",
            f"Fuel: {self.fuel:.1f}",
            f"Damage: {self.damage:.1f}",
            f"Laps: {self.laps}",
        ]


@dataclass
class CarRaceOutcome:
    race_time_seconds: float
    best_lap_seconds: float
    avg_speed: float
    damage_taken: float
    barrier_hits: int
    completed: bool


@dataclass
class CarRaceMemory:
    recent_outcomes: list[CarRaceOutcome] = field(default_factory=list)

    def remember(self, outcome: CarRaceOutcome) -> None:
        self.recent_outcomes.append(outcome)
        if len(self.recent_outcomes) > 10:
            self.recent_outcomes = self.recent_outcomes[-10:]

    def best_lap(self) -> float:
        laps = [entry.best_lap_seconds for entry in self.recent_outcomes if entry.best_lap_seconds > 0.0]
        if not laps:
            return 0.0
        return min(laps)


@dataclass
class CarBehaviorProfile:
    speed_priority: float = 1.0
    lap_improvement_priority: float = 0.9
    damage_avoidance_priority: float = 0.7
    keep_nose_forward_priority: float = 0.7
    avoid_slowdown_priority: float = 0.8
    barrier_avoidance_priority: float = 0.8
    risk_tolerance: float = 0.6


@dataclass
class CarLearningState:
    target_speed_bias: float = 1.0
    steering_aggression: float = 1.0
    safety_bias: float = 1.0

    def adapt(self, profile: CarBehaviorProfile, memory: CarRaceMemory) -> None:
        if not memory.recent_outcomes:
            return

        recent = memory.recent_outcomes[-3:]
        latest = memory.recent_outcomes[-1]
        avg_damage = sum(item.damage_taken for item in recent) / len(recent)
        avg_hits = sum(item.barrier_hits for item in recent) / len(recent)
        completed_ratio = sum(1 for item in recent if item.completed) / len(recent)

        historical_laps = [
            item.best_lap_seconds
            for item in memory.recent_outcomes[:-1]
            if item.best_lap_seconds > 0.0
        ]
        prev_best = min(historical_laps) if historical_laps else 0.0
        latest_lap = latest.best_lap_seconds if latest.best_lap_seconds > 0.0 else 0.0

        # Speed bias adapts incrementally instead of snapping to a fixed profile-derived value.
        base_speed = 1.0 + (profile.speed_priority + profile.avoid_slowdown_priority) * 0.03
        speed_delta = 0.0
        if latest.completed:
            speed_delta += 0.01
        else:
            speed_delta -= 0.015
        if latest_lap > 0.0 and prev_best > 0.0:
            if latest_lap < prev_best * 0.99:
                speed_delta += 0.02
            elif latest_lap > prev_best * 1.01:
                speed_delta -= 0.015
        speed_delta -= min(0.03, avg_hits * 0.004)
        self.target_speed_bias = min(
            1.45,
            max(
                0.72,
                self.target_speed_bias * 0.9 + base_speed * 0.1 + speed_delta,
            ),
        )

        # Safety rises with damage/hits/non-completions, and relaxes when runs are clean.
        safety_delta = 0.0
        safety_delta += (avg_damage / 100.0) * 0.08
        safety_delta += min(0.04, avg_hits * 0.006)
        safety_delta += (1.0 - completed_ratio) * 0.04
        if avg_damage < 18.0 and avg_hits < 1.5 and completed_ratio > 0.66:
            safety_delta -= 0.03
        self.safety_bias = min(1.6, max(0.7, self.safety_bias + safety_delta))

        # Steering aggression balances risk appetite against hit/damage feedback.
        aggression_base = 1.0 + profile.risk_tolerance * 0.12
        aggr_delta = aggression_base * 0.02
        aggr_delta -= min(0.04, avg_hits * 0.006)
        aggr_delta -= min(0.03, (avg_damage / 100.0) * 0.05)
        if latest_lap > 0.0 and prev_best > 0.0 and latest_lap < prev_best * 0.99:
            aggr_delta += 0.01
        self.steering_aggression = min(1.35, max(0.75, self.steering_aggression + aggr_delta))
