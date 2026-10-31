"""HTTP client for the Ollama API, using only the standard library."""

lazy import http.client
lazy import ssl

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from brain.domain.agents import AgentName, InstalledAgent, MountedAgent, parse_capabilities
from brain.domain.conversation import BackendChat, ChatChunk, ChatMessage
from brain.domain.errors import AgentNotInstalled, RuntimeRequestFailed, RuntimeUnavailable
from brain.ports import PullProgress

_CONNECT_TIMEOUT_SECONDS = 3.0
_QUICK_READ_TIMEOUT_SECONDS = 30.0
# Loading a big model from a slow SD card, or a first token on a Pi, can take many minutes.
_LONG_READ_TIMEOUT_SECONDS = 1800.0
_PIN_FOREVER = -1
_UNLOAD_NOW = 0
_PINNED_HORIZON = timedelta(days=365 * 10)


class OllamaClient:
    """Satisfies RuntimeProbe, ModelStore, ModelLoader, ChatBackend and EmbeddingBackend."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        self._secure = parts.scheme == "https"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._secure else 11434)
        self.base_url = base_url
        self._capability_cache: dict[str, frozenset] = {}
        self._cache_lock = threading.Lock()

    def version(self) -> str | None:
        try:
            return self._request_json("GET", "/api/version").get("version")
        except RuntimeUnavailable, RuntimeRequestFailed:
            return None

    def list_installed(self) -> tuple[InstalledAgent, ...]:
        models = self._request_json("GET", "/api/tags").get("models") or ()
        return tuple(self._to_installed(model) for model in models)

    def list_loaded(self) -> tuple[MountedAgent, ...]:
        models = self._request_json("GET", "/api/ps").get("models") or ()
        return tuple(self._to_mounted(model) for model in models if "remote_host" not in model)

    def pull(self, name: AgentName) -> Iterator[PullProgress]:
        for line in self._stream("POST", "/api/pull", {"model": name}):
            # Pull failures arrive inside a 200 response, as a line in the stream.
            if error := line.get("error"):
                raise RuntimeRequestFailed(f"Could not download {name}: {error}")
            yield PullProgress(
                status=line.get("status", ""),
                completed=line.get("completed"),
                total=line.get("total"),
            )

    def delete(self, name: AgentName) -> None:
        self._request_json("DELETE", "/api/delete", {"model": name}, not_found_is_agent=name)
        with self._cache_lock:
            self._capability_cache.pop(name, None)

    def load(self, name: AgentName, *, embedding: bool = False) -> None:
        self._set_keep_alive(name, _PIN_FOREVER, embedding=embedding)

    def unload(self, name: AgentName, *, embedding: bool = False) -> None:
        self._set_keep_alive(name, _UNLOAD_NOW, embedding=embedding)

    def _set_keep_alive(self, name: AgentName, keep_alive: int, *, embedding: bool) -> None:
        # Embedding-only models reject /api/generate, so they use their own endpoint.
        path, body = (
            ("/api/embed", {"model": name, "input": [], "keep_alive": keep_alive})
            if embedding
            else ("/api/generate", {"model": name, "keep_alive": keep_alive, "stream": False})
        )
        self._request_json(
            "POST", path, body, read_timeout=_LONG_READ_TIMEOUT_SECONDS, not_found_is_agent=name
        )

    def chat(self, request: BackendChat) -> Iterator[ChatChunk]:
        body: dict[str, Any] = {
            "model": request.agent,
            "messages": [self._to_wire_message(message) for message in request.messages],
            "stream": True,
            # Without this a chat could shorten the pin that mounting placed on the model.
            "keep_alive": _PIN_FOREVER,
        }
        if request.options:
            body["options"] = request.options
        if request.think is not None:
            body["think"] = request.think
        if request.format is not None:
            body["format"] = request.format
        if request.tools:
            body["tools"] = request.tools

        for line in self._stream("POST", "/api/chat", body, not_found_is_agent=request.agent):
            if error := line.get("error"):
                raise RuntimeRequestFailed(error)
            yield self._to_chunk(line)

    def embed(self, name: AgentName, inputs: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        response = self._request_json(
            "POST",
            "/api/embed",
            {"model": name, "input": list(inputs), "keep_alive": _PIN_FOREVER},
            read_timeout=_LONG_READ_TIMEOUT_SECONDS,
            not_found_is_agent=name,
        )
        return tuple(tuple(vector) for vector in response.get("embeddings") or ())

    def _to_installed(self, model: Mapping[str, Any]) -> InstalledAgent:
        name = AgentName.parse(model["name"])
        details = model.get("details") or {}
        return InstalledAgent(
            name=name,
            size_bytes=model.get("size", 0),
            family=details.get("family") or name.model,
            digest=model.get("digest", ""),
            modified_at=_parse_time(model.get("modified_at")),
            parameter_size=details.get("parameter_size") or None,
            quantization=details.get("quantization_level") or None,
            context_length=details.get("context_length"),
            capabilities=self._capabilities_of(name, model),
            remote=bool(model.get("remote_model") or model.get("remote_host")),
        )

    def _capabilities_of(self, name: AgentName, model: Mapping[str, Any]) -> frozenset:
        if "capabilities" in model:
            return parse_capabilities(model["capabilities"])
        # Older Ollama versions only report capabilities through /api/show.
        with self._cache_lock:
            cached = self._capability_cache.get(name)
        if cached is not None:
            return cached
        try:
            shown = self._request_json("POST", "/api/show", {"model": name})
        except RuntimeRequestFailed:
            return frozenset()
        capabilities = parse_capabilities(shown.get("capabilities"))
        with self._cache_lock:
            self._capability_cache[name] = capabilities
        return capabilities

    @staticmethod
    def _to_mounted(model: Mapping[str, Any]) -> MountedAgent:
        expires = _parse_time(model.get("expires_at"))
        return MountedAgent(
            name=AgentName.parse(model["name"]),
            memory_bytes=model.get("size", 0),
            vram_bytes=model.get("size_vram", 0),
            expires_at=expires,
            pinned=expires is not None and expires > datetime.now(UTC) + _PINNED_HORIZON,
            context_length=model.get("context_length"),
        )

    @staticmethod
    def _to_wire_message(message: ChatMessage) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.images:
            wire["images"] = list(message.images)
        if message.tool_calls:
            wire["tool_calls"] = list(message.tool_calls)
        if message.tool_name:
            wire["tool_name"] = message.tool_name
        return wire

    @staticmethod
    def _to_chunk(line: Mapping[str, Any]) -> ChatChunk:
        message = line.get("message") or {}
        return ChatChunk(
            content=message.get("content") or "",
            thinking=message.get("thinking") or "",
            tool_calls=tuple(
                json.loads(json.dumps(call), object_pairs_hook=frozendict, array_hook=tuple)
                for call in message.get("tool_calls") or ()
            ),
            done=bool(line.get("done")),
            done_reason=line.get("done_reason"),
            prompt_tokens=line.get("prompt_eval_count"),
            completion_tokens=line.get("eval_count"),
            completion_nanos=line.get("eval_duration"),
            total_nanos=line.get("total_duration"),
            load_nanos=line.get("load_duration"),
        )

    def _connect(self, read_timeout: float) -> http.client.HTTPConnection:
        if self._secure:
            connection = http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=_CONNECT_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                self._host, self._port, timeout=_CONNECT_TIMEOUT_SECONDS
            )
        try:
            connection.connect()
        except OSError as error:
            connection.close()
            raise RuntimeUnavailable(
                f"Ollama is not reachable at {self.base_url}.",
                hint="Start it, or install it from the dashboard.",
            ) from error
        assert connection.sock is not None
        connection.sock.settimeout(read_timeout)
        return connection

    def _send(
        self,
        connection: http.client.HTTPConnection,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
    ) -> http.client.HTTPResponse:
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if encoded else {}
        try:
            connection.request(method, path, body=encoded, headers=headers)
            return connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            raise RuntimeUnavailable(f"Lost contact with Ollama at {self.base_url}.") from error

    def _request_json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        read_timeout: float = _QUICK_READ_TIMEOUT_SECONDS,
        not_found_is_agent: str | None = None,
    ) -> dict[str, Any]:
        connection = self._connect(read_timeout)
        try:
            response = self._send(connection, method, path, body)
            try:
                raw = response.read()
            except (OSError, http.client.HTTPException) as error:
                raise RuntimeUnavailable(f"Lost contact with Ollama at {self.base_url}.") from error
            self._raise_for_status(response.status, raw, not_found_is_agent)
            return json.loads(raw) if raw else {}
        finally:
            connection.close()

    def _stream(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any],
        *,
        not_found_is_agent: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        connection = self._connect(_LONG_READ_TIMEOUT_SECONDS)
        # Closing the connection is what tells Ollama to stop generating when a client walks away.
        try:
            response = self._send(connection, method, path, body)
            if response.status >= 400:
                self._raise_for_status(response.status, response.read(), not_found_is_agent)
            while True:
                try:
                    line = response.readline()
                except (OSError, http.client.HTTPException) as error:
                    raise RuntimeUnavailable(
                        f"Lost contact with Ollama at {self.base_url}."
                    ) from error
                if not line:
                    return
                if line.strip():
                    yield json.loads(line)
        finally:
            connection.close()

    @staticmethod
    def _raise_for_status(status: int, raw: bytes, agent: str | None) -> None:
        if status < 400:
            return
        try:
            message = json.loads(raw).get("error") or raw.decode("utf-8", "replace")
        except ValueError:
            message = raw.decode("utf-8", "replace")
        if status == 404 and agent is not None:
            raise AgentNotInstalled(f"{agent} is not installed.", hint="Install it first.")
        raise RuntimeRequestFailed(message or f"Ollama answered with HTTP {status}.")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
