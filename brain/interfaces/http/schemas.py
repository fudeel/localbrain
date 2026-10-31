"""The exact shape of each API request body. Closed, so typos fail loudly instead of being ignored."""

from typing import Any, Literal, NotRequired, TypedDict


class MessagePayload(TypedDict, closed=True):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    images: NotRequired[list[str]]
    tool_calls: NotRequired[list[dict[str, Any]]]
    tool_name: NotRequired[str]


class ChatPayload(TypedDict, closed=True):
    message: NotRequired[str]
    messages: NotRequired[list[MessagePayload]]
    conversation_id: NotRequired[str]
    system: NotRequired[str | None]
    images: NotRequired[list[str]]
    options: NotRequired[dict[str, Any]]
    think: NotRequired[bool | str | None]
    format: NotRequired[str | dict[str, Any]]
    tools: NotRequired[list[dict[str, Any]]]
    stream: NotRequired[bool]
    auto_mount: NotRequired[bool]
    sensor_context: NotRequired[bool]


class ForcePayload(TypedDict, closed=True):
    force: NotRequired[bool]


class ResolvePayload(TypedDict, closed=True):
    name: str


class EmbedPayload(TypedDict, closed=True):
    input: str | list[str]


class RuntimeInstallPayload(TypedDict, closed=True):
    method: NotRequired[str]


class RenamePayload(TypedDict, closed=True):
    title: str


class ReadingPayload(TypedDict, closed=True):
    metric: str
    value: float | str | bool
    unit: NotRequired[str | None]


class DeviceReportPayload(TypedDict, closed=True):
    readings: list[ReadingPayload]
    name: NotRequired[str]
    kind: NotRequired[str]
    meta: NotRequired[dict[str, Any]]
