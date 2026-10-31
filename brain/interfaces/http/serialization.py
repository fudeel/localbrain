"""Turns domain values into JSON-ready data, and defines how they look on the wire."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

from brain.domain.agents import Agent
from brain.domain.jobs import Job
from brain.domain.runtime import RuntimeStatus


def to_jsonable(value: Any) -> Any:
    match value:
        case IntEnum():
            return value.name.lower()
        case Enum():
            return value.value
        case None | bool() | int() | float():
            return value
        case str():
            return str(value)
        case datetime():
            return value.isoformat()
        case Path():
            return str(value)
        case Agent():
            return _with_extras(value, can_chat=value.can_chat)
        case Job():
            return _with_extras(value, is_active=value.is_active)
        case RuntimeStatus():
            return _with_extras(value, installed=value.installed)
        case Mapping():
            return {str(key): to_jsonable(item) for key, item in value.items()}
        case set() | frozenset():
            return sorted(to_jsonable(item) for item in value)
        case list() | tuple():
            return [to_jsonable(item) for item in value]
        case _ if is_dataclass(value) and not isinstance(value, type):
            return _with_extras(value)
        case _:
            raise TypeError(f"Cannot serialize {type(value).__name__}")


def _with_extras(value: Any, **extras: Any) -> dict[str, Any]:
    data = {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    return data | extras
