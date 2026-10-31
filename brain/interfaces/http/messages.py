"""HTTP request and response primitives.

A response knows how to write itself, so the server treats a JSON body and an endless event
stream the same way and never needs to change when a new kind of response appears.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from brain.domain.errors import InvalidRequest
from brain.interfaces.http.serialization import to_jsonable

_SSE_HEARTBEAT_COMMENT = b": heartbeat\n\n"


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=frozendict)
    headers: Mapping[str, str] = field(default_factory=frozendict)
    params: Mapping[str, str] = field(default_factory=frozendict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except ValueError as error:
            raise InvalidRequest(f"The request body is not valid JSON: {error}") from error

    def flag(self, name: str) -> bool:
        return self.query.get(name, "").lower() in ("1", "true", "yes")

    def int_query(self, name: str, *, default: int, maximum: int) -> int:
        raw = self.query.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise InvalidRequest(f"Query parameter '{name}' must be a number.") from error
        return min(max(value, 1), maximum)


class ResponseChannel(Protocol):
    def begin(self, status: int, headers: Mapping[str, str]) -> None: ...

    def write(self, data: bytes) -> None: ...

    def write_chunk(self, data: bytes) -> None: ...

    def end_chunks(self) -> None: ...


class Response(Protocol):
    def send(self, channel: ResponseChannel) -> None: ...


@dataclass(frozen=True, slots=True)
class JsonResponse:
    data: Any
    status: int = 200

    def send(self, channel: ResponseChannel) -> None:
        body = json.dumps(to_jsonable(self.data), ensure_ascii=False).encode("utf-8")
        channel.begin(
            self.status,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        channel.write(body)


@dataclass(frozen=True, slots=True)
class EmptyResponse:
    status: int = 204

    def send(self, channel: ResponseChannel) -> None:
        channel.begin(self.status, {"Content-Length": "0"})


@dataclass(frozen=True, slots=True)
class AssetResponse:
    body: bytes
    content_type: str
    etag: str
    if_none_match: str | None = None
    extra_headers: Mapping[str, str] = field(default_factory=frozendict)

    def send(self, channel: ResponseChannel) -> None:
        headers = {"ETag": self.etag, "Cache-Control": "no-cache", **self.extra_headers}
        if self.if_none_match == self.etag:
            channel.begin(304, headers)
            return
        channel.begin(
            200,
            {**headers, "Content-Type": self.content_type, "Content-Length": str(len(self.body))},
        )
        channel.write(self.body)


@dataclass(frozen=True, slots=True)
class NdjsonStream:
    """One JSON object per line: the easiest streaming format for any program to consume."""

    items: Iterator[Any]

    def send(self, channel: ResponseChannel) -> None:
        channel.begin(
            200,
            {
                "Content-Type": "application/x-ndjson",
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-store",
            },
        )
        try:
            for item in self.items:
                line = json.dumps(to_jsonable(item), ensure_ascii=False) + "\n"
                channel.write_chunk(line.encode("utf-8"))
            channel.end_chunks()
        finally:
            # Closing the generator now is what stops the agent when a client disconnects.
            close = getattr(self.items, "close", None)
            if close is not None:
                close()


@dataclass(frozen=True, slots=True)
class SseStream:
    """Server-sent events. `None` from the source means idle, so a heartbeat goes out instead."""

    events: Iterator[tuple[str, str, Any] | None]

    def send(self, channel: ResponseChannel) -> None:
        channel.begin(
            200,
            {
                "Content-Type": "text/event-stream",
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        try:
            channel.write_chunk(b": connected\n\n")
            for item in self.events:
                if item is None:
                    channel.write_chunk(_SSE_HEARTBEAT_COMMENT)
                    continue
                event_id, event_type, data = item
                payload = json.dumps(to_jsonable(data), ensure_ascii=False)
                message = f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"
                channel.write_chunk(message.encode("utf-8"))
        finally:
            close = getattr(self.events, "close", None)
            if close is not None:
                close()
