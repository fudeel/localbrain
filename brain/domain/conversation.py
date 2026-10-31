"""Talking to agents: messages, requests and the events a reply is made of."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Distinguishes "leave the stored prompt alone" from `None`, which clears it.
UNSET = sentinel("UNSET")


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMessage:
    role: Role
    content: str
    thinking: str | None = None
    images: tuple[str, ...] = ()
    tool_calls: tuple[frozendict[str, Any], ...] = ()
    tool_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMessage:
    id: int
    conversation_id: str
    role: Role
    content: str
    thinking: str | None
    agent: str | None
    created_at: datetime
    interrupted: bool = False
    stats: frozendict[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Conversation:
    id: str
    title: str
    agent: str
    system: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatRequest:
    agent: str
    message: str | None = None
    messages: tuple[ChatMessage, ...] = ()
    conversation_id: str | None = None
    system: str | None | UNSET = UNSET
    images: tuple[str, ...] = ()
    options: frozendict[str, Any] = frozendict()
    think: bool | str | None = None
    format: str | frozendict[str, Any] | None = None
    tools: tuple[frozendict[str, Any], ...] = ()
    auto_mount: bool = False
    sensor_context: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendChat:
    """A fully assembled call to the model runtime."""

    agent: str
    messages: tuple[ChatMessage, ...]
    options: frozendict[str, Any] = frozendict()
    think: bool | str | None = None
    format: str | frozendict[str, Any] | None = None
    tools: tuple[frozendict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatChunk:
    content: str = ""
    thinking: str = ""
    tool_calls: tuple[frozendict[str, Any], ...] = ()
    done: bool = False
    done_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    completion_nanos: int | None = None
    total_nanos: int | None = None
    load_nanos: int | None = None


class ChatEventType(StrEnum):
    START = "start"
    THINKING = "thinking"
    CONTENT = "content"
    TOOL_CALLS = "tool_calls"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatEvent:
    type: ChatEventType
    text: str = ""
    conversation_id: str | None = None
    tool_calls: tuple[frozendict[str, Any], ...] = ()
    stats: frozendict[str, Any] | None = None
    error: frozendict[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatReply:
    """A complete reply, for clients that do not want to stream."""

    agent: str
    content: str
    thinking: str
    conversation_id: str | None
    tool_calls: tuple[frozendict[str, Any], ...] = ()
    stats: frozendict[str, Any] | None = None
