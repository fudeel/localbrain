"""Runtime checks driven by closed TypedDicts (PEP 728).

The same class serves as the static type and the runtime schema, so they cannot drift apart.
"""

import types
from collections.abc import Mapping
from typing import Any, Literal, Union, cast, get_args, get_origin, get_type_hints, is_typeddict

from brain.domain.errors import InvalidRequest


def validate[T](schema: type[T], data: object, *, path: str = "body") -> T:
    if not isinstance(data, Mapping):
        raise InvalidRequest(f"{path} must be a JSON object.")
    hints = get_type_hints(schema)
    if getattr(schema, "__closed__", False):
        for key in data.keys() - hints.keys():
            raise InvalidRequest(
                f"{path} has an unknown field '{key}'.",
                hint=f"Allowed fields: {', '.join(sorted(hints))}.",
            )
    for key in schema.__required_keys__ - data.keys():
        raise InvalidRequest(f"{path} is missing the required field '{key}'.")
    for key, value in data.items():
        _check(hints[key], value, f"{path}.{key}")
    return cast(T, data)


def _check(hint: Any, value: object, path: str) -> None:
    if not _matches(hint, value, path):
        raise InvalidRequest(f"{path} must be {_describe(hint)}.")


def _matches(hint: Any, value: object, path: str) -> bool:
    origin = get_origin(hint)
    if hint is Any:
        return True
    if origin in (Union, types.UnionType):
        return any(_matches_quietly(option, value, path) for option in get_args(hint))
    if origin is Literal:
        return value in get_args(hint)
    if origin is list:
        if not isinstance(value, list):
            return False
        item = get_args(hint)[0]
        for index, entry in enumerate(value):
            _check(item, entry, f"{path}[{index}]")
        return True
    if origin is dict:
        return isinstance(value, Mapping)
    if is_typeddict(hint):
        validate(hint, value, path=path)
        return True
    if hint is types.NoneType:
        return value is None
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, hint)


def _matches_quietly(hint: Any, value: object, path: str) -> bool:
    try:
        return _matches(hint, value, path)
    except InvalidRequest:
        return False


def _describe(hint: Any) -> str:
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        return " or ".join(_describe(option) for option in get_args(hint))
    if origin is Literal:
        return "one of " + ", ".join(repr(option) for option in get_args(hint))
    if origin is list:
        return "a list"
    if origin is dict or is_typeddict(hint):
        return "an object"
    if hint is types.NoneType:
        return "null"
    return {str: "a string", bool: "true or false", int: "an integer", float: "a number"}.get(
        hint, str(hint)
    )
