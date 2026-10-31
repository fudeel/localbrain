"""Things connected to the brain: sensors, programs, and whatever comes next."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


type ReadingValue = float | int | str | bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    metric: str
    value: ReadingValue
    unit: str | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Device:
    id: str
    name: str
    kind: str
    meta: frozendict[str, Any]
    first_seen: datetime
    last_seen: datetime
    latest: tuple[Reading, ...] = ()
