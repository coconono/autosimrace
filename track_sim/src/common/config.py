from __future__ import annotations

from pathlib import Path


def read_simple_conf(path: Path, defaults: dict[str, str]) -> dict[str, str]:
    values = dict(defaults)
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, raw_value = cleaned.split("=", 1)
        values[key.strip()] = raw_value.strip()
    return values


def as_int(config: dict[str, str], key: str, fallback: int) -> int:
    try:
        return int(config.get(key, fallback))
    except (TypeError, ValueError):
        return fallback
