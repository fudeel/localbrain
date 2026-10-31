"""Something happened in the brain. Connected programs can subscribe to these."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _new_id() -> str:
    return str(uuid.uuid7())


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    type: str
    data: frozendict[str, Any] = frozendict()
    id: str = field(default_factory=_new_id)
    at: datetime = field(default_factory=_now)
