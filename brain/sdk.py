"""A small client for your own programs. Copy this one file anywhere; it has no dependencies.

    from brain.sdk import BrainClient

    brain = BrainClient("http://127.0.0.1:8484")
    print(brain.ask("qwen3:4b-q4_K_M", "Tell my kids a joke about robots."))
    brain.report("kitchen", temperature=(21.5, "°C"), humidity=(40, "%"))
"""

lazy import http.client

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

_DEFAULT_TIMEOUT_SECONDS = 1800.0


class BrainApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(f"{message} ({hint})" if hint else message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint


class BrainClient:
    def __init__(
        self,
        url: str = "http://127.0.0.1:8484",
        *,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        parts = urlsplit(url)
        self._secure = parts.scheme == "https"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._secure else 80)
        self._token = token or os.environ.get("BRAIN_TOKEN")
        self._timeout = timeout

    def hardware(self) -> dict[str, Any]:
        return self._json("GET", "/hardware")

    def runtime(self) -> dict[str, Any]:
        return self._json("GET", "/runtime")

    def agents(self) -> list[dict[str, Any]]:
        return self._json("GET", "/agents")["agents"]

    def agent(self, name: str) -> dict[str, Any]:
        return self._json("GET", f"/agents/{_path(name)}")

    def install(self, name: str, *, force: bool = False) -> dict[str, Any]:
        return self._lifecycle("POST", f"/agents/{_path(name)}/install", {"force": force})

    def mount(self, name: str, *, force: bool = False) -> dict[str, Any]:
        return self._lifecycle("POST", f"/agents/{_path(name)}/mount", {"force": force})

    def unmount(self, name: str) -> dict[str, Any]:
        return self._lifecycle("POST", f"/agents/{_path(name)}/unmount")

    def uninstall(self, name: str) -> dict[str, Any]:
        return self._lifecycle("DELETE", f"/agents/{_path(name)}")

    def ask(self, agent: str, message: str, **fields: Any) -> str:
        """The simplest way to talk to an agent: a question in, the answer's text out.

        Skips the reasoning step unless asked, as it can take minutes on a CPU.
        """
        fields.setdefault("think", False)
        return self.chat(agent, message=message, **fields)["content"]

    def chat(self, agent: str, **fields: Any) -> dict[str, Any]:
        """See the README for all fields, such as tools, format, system and conversation_id."""
        return self._json("POST", f"/agents/{_path(agent)}/chat", {**fields, "stream": False})

    def stream(self, agent: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Yields events as they arrive. Stopping early also stops the agent generating."""
        response, connection = self._open(
            "POST", f"/agents/{_path(agent)}/chat", {**fields, "stream": True}
        )
        try:
            while line := response.readline():
                if line.strip():
                    yield json.loads(line)
        finally:
            connection.close()

    def embed(self, agent: str, inputs: str | Sequence[str]) -> list[list[float]]:
        payload = inputs if isinstance(inputs, str) else list(inputs)
        return self._json("POST", f"/agents/{_path(agent)}/embed", {"input": payload})["embeddings"]

    def report(
        self,
        device: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        **metrics: float | str | bool | tuple[float | str | bool, str],
    ) -> dict[str, Any]:
        """Sends readings. Give a bare value, or `(value, unit)` when it has a unit."""
        readings = [
            {"metric": metric, "value": value[0], "unit": value[1]}
            if isinstance(value, tuple)
            else {"metric": metric, "value": value}
            for metric, value in metrics.items()
        ]
        body: dict[str, Any] = {"readings": readings}
        if name:
            body["name"] = name
        if kind:
            body["kind"] = kind
        return self._json("POST", f"/devices/{_path(device)}/readings", body)

    def devices(self) -> list[dict[str, Any]]:
        return self._json("GET", "/devices")["devices"]

    def events(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Follows everything happening in the brain, as `(event_type, data)`. Runs forever."""
        response, connection = self._open("GET", "/events", None)
        event_type = "message"
        try:
            while line := response.readline():
                text = line.decode("utf-8").rstrip("\n")
                if text.startswith("event: "):
                    event_type = text.removeprefix("event: ")
                elif text.startswith("data: "):
                    yield event_type, json.loads(text.removeprefix("data: "))
        finally:
            connection.close()

    def _lifecycle(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        job = self._json(method, f"{path}?wait=1", body)
        if job.get("state") == "failed":
            raise BrainApiError(500, "job_failed", job.get("error") or "The operation failed.")
        return job

    def _json(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        response, connection = self._open(method, path, body)
        try:
            raw = response.read()
        finally:
            connection.close()
        return json.loads(raw) if raw else None

    def _open(self, method: str, path: str, body: Mapping[str, Any] | None):
        factory = http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        connection = factory(self._host, self._port, timeout=self._timeout)
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, f"/api/v1{path}", body=encoded, headers=headers)
        response = connection.getresponse()
        if response.status >= 400:
            raw = response.read()
            connection.close()
            raise _to_error(response.status, raw)
        return response, connection


def _path(value: str) -> str:
    return quote(value, safe="")


def _to_error(status: int, raw: bytes) -> BrainApiError:
    try:
        error = json.loads(raw)["error"]
        return BrainApiError(status, error["code"], error["message"], error.get("hint"))
    except KeyError, TypeError, ValueError:
        return BrainApiError(status, "http_error", raw.decode("utf-8", "replace"))
