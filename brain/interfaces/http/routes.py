"""One controller per resource. Controllers translate HTTP to service calls and back, nothing more."""

lazy import hashlib

from collections.abc import Iterator
from importlib import resources
from typing import Any

from brain import __version__
from brain.domain.conversation import UNSET, ChatMessage, ChatRequest, Role
from brain.domain.devices import Reading
from brain.domain.errors import InvalidRequest, NotFound
from brain.domain.events import Event
from brain.domain.jobs import Job
from brain.interfaces.http.messages import (
    AssetResponse,
    EmptyResponse,
    JsonResponse,
    NdjsonStream,
    Request,
    Response,
    SseStream,
)
from brain.interfaces.http.router import Router
from brain.interfaces.http.schemas import (
    ChatPayload,
    DeviceReportPayload,
    EmbedPayload,
    ForcePayload,
    MessagePayload,
    RenamePayload,
    ResolvePayload,
    RuntimeInstallPayload,
)
from brain.ports import EventBus, HardwareInspector
from brain.services.agents import AgentManager
from brain.services.chat import ChatService
from brain.services.devices import DeviceHub
from brain.services.jobs import JobRunner
from brain.services.runtime import RuntimeManager
from brain.validation import validate

API = "/api/v1"
_HEARTBEAT_SECONDS = 15.0
_WAIT_LIMIT_SECONDS = 1800.0
_ACCEPTED = 202

_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _job_response(jobs: JobRunner, job: Job, request: Request) -> Response:
    """Background work answers immediately, unless the caller asks to wait with ?wait=1."""
    if request.flag("wait"):
        return JsonResponse(jobs.wait(job.id, _WAIT_LIMIT_SECONDS))
    return JsonResponse(job, status=_ACCEPTED)


class SystemController:
    def __init__(
        self,
        *,
        runtime: RuntimeManager,
        hardware: HardwareInspector,
        agents: AgentManager,
        jobs: JobRunner,
        token_required: bool,
    ) -> None:
        self._runtime = runtime
        self._hardware = hardware
        self._agents = agents
        self._jobs = jobs
        self._token_required = token_required

    def register(self, router: Router) -> None:
        router.add("GET", f"{API}/health", self.health, public=True)
        router.add("GET", f"{API}/runtime", self.runtime_status)
        router.add("POST", f"{API}/runtime/install", self.runtime_install)
        router.add("POST", f"{API}/runtime/start", self.runtime_start)
        router.add("GET", f"{API}/hardware", self.hardware)

    def health(self, request: Request) -> Response:
        return JsonResponse(
            {"ok": True, "version": __version__, "token_required": self._token_required}
        )

    def runtime_status(self, request: Request) -> Response:
        return JsonResponse(self._runtime.status())

    def runtime_install(self, request: Request) -> Response:
        payload = validate(RuntimeInstallPayload, request.json())
        return _job_response(self._jobs, self._runtime.install(method=payload.get("method")), request)

    def runtime_start(self, request: Request) -> Response:
        return _job_response(self._jobs, self._runtime.start(), request)

    def hardware(self, request: Request) -> Response:
        return JsonResponse(self._hardware.snapshot())


class AgentController:
    def __init__(self, *, agents: AgentManager, jobs: JobRunner) -> None:
        self._agents = agents
        self._jobs = jobs

    def register(self, router: Router) -> None:
        router.add("GET", f"{API}/agents", self.list)
        router.add("POST", f"{API}/agents/resolve", self.resolve)
        router.add("POST", f"{API}/agents/{{name+}}/install", self.install)
        router.add("POST", f"{API}/agents/{{name+}}/mount", self.mount)
        router.add("POST", f"{API}/agents/{{name+}}/unmount", self.unmount)
        router.add("POST", f"{API}/agents/{{name+}}/embed", self.embed)
        router.add("GET", f"{API}/agents/{{name+}}", self.get)
        router.add("DELETE", f"{API}/agents/{{name+}}", self.uninstall)

    def list(self, request: Request) -> Response:
        return JsonResponse(self._agents.overview())

    def get(self, request: Request) -> Response:
        return JsonResponse(self._agents.get(request.params["name"]))

    def resolve(self, request: Request) -> Response:
        payload = validate(ResolvePayload, request.json())
        return JsonResponse(self._agents.get(payload["name"]))

    def install(self, request: Request) -> Response:
        payload = validate(ForcePayload, request.json())
        job = self._agents.install(request.params["name"], force=payload.get("force", False))
        return _job_response(self._jobs, job, request)

    def mount(self, request: Request) -> Response:
        payload = validate(ForcePayload, request.json())
        job = self._agents.mount(request.params["name"], force=payload.get("force", False))
        return _job_response(self._jobs, job, request)

    def unmount(self, request: Request) -> Response:
        return _job_response(self._jobs, self._agents.unmount(request.params["name"]), request)

    def uninstall(self, request: Request) -> Response:
        return _job_response(self._jobs, self._agents.uninstall(request.params["name"]), request)

    def embed(self, request: Request) -> Response:
        payload = validate(EmbedPayload, request.json())
        raw = payload["input"]
        inputs = (raw,) if isinstance(raw, str) else tuple(raw)
        return JsonResponse(
            {"embeddings": self._agents.embed(request.params["name"], inputs)}
        )


class ChatController:
    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    def register(self, router: Router) -> None:
        router.add("POST", f"{API}/agents/{{name+}}/chat", self.chat)
        router.add("GET", f"{API}/conversations", self.list_conversations)
        router.add("GET", f"{API}/conversations/{{conversation_id}}", self.get_conversation)
        router.add("PATCH", f"{API}/conversations/{{conversation_id}}", self.rename_conversation)
        router.add("DELETE", f"{API}/conversations/{{conversation_id}}", self.delete_conversation)

    def chat(self, request: Request) -> Response:
        payload = validate(ChatPayload, request.json())
        chat_request = self._to_chat_request(request.params["name"], payload)
        if payload.get("stream", False):
            return NdjsonStream(self._chat.stream(chat_request))
        return JsonResponse(self._chat.complete(chat_request))

    def list_conversations(self, request: Request) -> Response:
        limit = request.int_query("limit", default=100, maximum=500)
        return JsonResponse({"conversations": self._chat.conversations(limit=limit)})

    def get_conversation(self, request: Request) -> Response:
        conversation, messages = self._chat.conversation(request.params["conversation_id"])
        return JsonResponse({"conversation": conversation, "messages": messages})

    def rename_conversation(self, request: Request) -> Response:
        payload = validate(RenamePayload, request.json())
        return JsonResponse(
            self._chat.rename_conversation(request.params["conversation_id"], payload["title"])
        )

    def delete_conversation(self, request: Request) -> Response:
        self._chat.delete_conversation(request.params["conversation_id"])
        return EmptyResponse()

    @staticmethod
    def _to_chat_request(agent: str, payload: ChatPayload) -> ChatRequest:
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not 0 < len(conversation_id) <= 128:
            raise InvalidRequest("conversation_id must be between 1 and 128 characters.")
        return ChatRequest(
            agent=agent,
            message=payload.get("message"),
            messages=tuple(_to_chat_message(item) for item in payload.get("messages", ())),
            conversation_id=conversation_id,
            system=payload["system"] if "system" in payload else UNSET,
            images=tuple(payload.get("images", ())),
            options=_deep_freeze(payload.get("options", {})),
            think=payload.get("think"),
            format=_deep_freeze(payload["format"]) if "format" in payload else None,
            tools=tuple(_deep_freeze(tool) for tool in payload.get("tools", ())),
            auto_mount=payload.get("auto_mount", False),
            sensor_context=payload.get("sensor_context", False),
        )


def _to_chat_message(item: MessagePayload) -> ChatMessage:
    return ChatMessage(
        role=Role(item["role"]),
        content=item["content"],
        images=tuple(item.get("images", ())),
        tool_calls=tuple(_deep_freeze(call) for call in item.get("tool_calls", ())),
        tool_name=item.get("tool_name"),
    )


def _deep_freeze(value: Any) -> Any:
    match value:
        case dict():
            return frozendict({key: _deep_freeze(item) for key, item in value.items()})
        case list():
            return tuple(_deep_freeze(item) for item in value)
        case _:
            return value


class DeviceController:
    def __init__(self, devices: DeviceHub) -> None:
        self._devices = devices

    def register(self, router: Router) -> None:
        router.add("GET", f"{API}/devices", self.list)
        router.add("POST", f"{API}/devices/{{device_id}}/readings", self.report)
        router.add("GET", f"{API}/devices/{{device_id}}/readings", self.history)
        router.add("DELETE", f"{API}/devices/{{device_id}}", self.remove)

    def list(self, request: Request) -> Response:
        return JsonResponse({"devices": self._devices.devices()})

    def report(self, request: Request) -> Response:
        payload = validate(DeviceReportPayload, request.json())
        readings = tuple(
            Reading(metric=item["metric"], value=item["value"], unit=item.get("unit"))
            for item in payload["readings"]
        )
        device = self._devices.report(
            request.params["device_id"],
            readings=readings,
            name=payload.get("name"),
            kind=payload.get("kind"),
            meta=payload.get("meta"),
        )
        return JsonResponse(device, status=201)

    def history(self, request: Request) -> Response:
        limit = request.int_query("limit", default=200, maximum=5000)
        readings = self._devices.history(
            request.params["device_id"], metric=request.query.get("metric"), limit=limit
        )
        return JsonResponse({"readings": readings})

    def remove(self, request: Request) -> Response:
        self._devices.remove(request.params["device_id"])
        return EmptyResponse()


class JobController:
    def __init__(self, jobs: JobRunner) -> None:
        self._jobs = jobs

    def register(self, router: Router) -> None:
        router.add("GET", f"{API}/jobs", self.list)
        router.add("GET", f"{API}/jobs/{{job_id}}", self.get)
        router.add("DELETE", f"{API}/jobs/{{job_id}}", self.cancel)

    def list(self, request: Request) -> Response:
        return JsonResponse({"jobs": self._jobs.all()})

    def get(self, request: Request) -> Response:
        return JsonResponse(self._jobs.get(request.params["job_id"]))

    def cancel(self, request: Request) -> Response:
        return JsonResponse(self._jobs.cancel(request.params["job_id"]), status=_ACCEPTED)


class EventController:
    """Lets any connected program follow what happens in the brain as it happens."""

    def __init__(self, events: EventBus) -> None:
        self._events = events

    def register(self, router: Router) -> None:
        router.add("GET", f"{API}/events", self.stream)

    def stream(self, request: Request) -> Response:
        return SseStream(self._follow())

    def _follow(self) -> Iterator[tuple[str, str, Any] | None]:
        subscription = self._events.subscribe()
        try:
            while True:
                event: Event | None = subscription.next(_HEARTBEAT_SECONDS)
                if event is None:
                    yield None
                else:
                    yield event.id, event.type, {"at": event.at, **event.data}
        finally:
            subscription.close()


class DashboardController:
    """Serves the web UI from memory. Paths are looked up, never joined onto the filesystem."""

    def __init__(self) -> None:
        self._assets = self._load_assets()

    def register(self, router: Router) -> None:
        router.add("GET", "/", self._serve_index, public=True)
        for path in self._assets:
            router.add("GET", path, self._serve_asset, public=True)

    def _serve_index(self, request: Request) -> Response:
        return self._respond("/index.html", request)

    def _serve_asset(self, request: Request) -> Response:
        return self._respond(request.path, request)

    def _respond(self, path: str, request: Request) -> Response:
        asset = self._assets.get(path)
        if asset is None:
            raise NotFound(f"Nothing lives at {path}.")
        body, content_type, etag = asset
        return AssetResponse(
            body=body,
            content_type=content_type,
            etag=etag,
            if_none_match=request.headers.get("if-none-match"),
            extra_headers=frozendict({"Content-Security-Policy": _CONTENT_SECURITY_POLICY}),
        )

    @classmethod
    def _load_assets(cls) -> dict[str, tuple[bytes, str, str]]:
        assets: dict[str, tuple[bytes, str, str]] = {}
        cls._collect(resources.files("brain.web"), "", assets)
        return assets

    @classmethod
    def _collect(cls, directory, prefix: str, assets: dict) -> None:
        for entry in directory.iterdir():
            if entry.is_dir():
                if not entry.name.startswith("__"):
                    cls._collect(entry, f"{prefix}/{entry.name}", assets)
                continue
            suffix = entry.name[entry.name.rfind(".") :]
            content_type = _CONTENT_TYPES.get(suffix)
            if content_type is None:
                continue
            body = entry.read_bytes()
            etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
            assets[f"{prefix}/{entry.name}"] = (body, content_type, etag)
