from __future__ import annotations

import json
from pathlib import Path

from .models import CarConfig, TrackLayout


def save_track(path: Path, track: TrackLayout) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(track.to_dict(), indent=2), encoding="utf-8")


def load_track(path: Path) -> TrackLayout:
    data = json.loads(path.read_text(encoding="utf-8"))
    return TrackLayout.from_dict(data)


def save_car(path: Path, car: CarConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(car.to_dict(), indent=2), encoding="utf-8")


def load_car(path: Path) -> CarConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    return CarConfig.from_dict(data)
