"""The contracts services depend on. Concrete adapters live in `brain.infrastructure`.

Interfaces stay deliberately small so a service only sees what it needs:
`OllamaClient` satisfies four of these, yet `ChatService` knows about none of its pull logic.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from brain.domain.agents import AgentName, AgentSpec, InstalledAgent, MountedAgent
from brain.domain.conversation import UNSET, BackendChat, ChatChunk, Conversation, StoredMessage
from brain.domain.devices import Device, Reading
from brain.domain.events import Event
from brain.domain.hardware import HardwareProfile


class Probe[T](Protocol):
    def read(self) -> T: ...


class HardwareInspector(Protocol):
    def snapshot(self) -> HardwareProfile: ...


class AgentCatalog(Protocol):
    def all(self) -> tuple[AgentSpec, ...]: ...

    def find(self, name: AgentName) -> AgentSpec | None: ...


class AgentRegistry(Protocol):
    """Looks up agents the curated catalog has never heard of."""

    def lookup(self, name: AgentName) -> AgentSpec | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PullProgress:
    status: str
    completed: int | None = None
    total: int | None = None


class RuntimeProbe(Protocol):
    def version(self) -> str | None: ...


class ModelStore(Protocol):
    def list_installed(self) -> tuple[InstalledAgent, ...]: ...

    def pull(self, name: AgentName) -> Iterator[PullProgress]: ...

    def delete(self, name: AgentName) -> None: ...


class ModelLoader(Protocol):
    def list_loaded(self) -> tuple[MountedAgent, ...]: ...

    def load(self, name: AgentName, *, embedding: bool = False) -> None: ...

    def unload(self, name: AgentName, *, embedding: bool = False) -> None: ...


class ChatBackend(Protocol):
    def chat(self, request: BackendChat) -> Iterator[ChatChunk]: ...


class EmbeddingBackend(Protocol):
    def embed(self, name: AgentName, inputs: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class ProgressSink(Protocol):
    """How long-running work reports back and learns it should stop."""

    def report(
        self,
        message: str | None = None,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None: ...

    def raise_if_cancelled(self) -> None: ...


class RuntimeInstaller(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def label(self) -> str: ...

    def availability(self) -> tuple[bool, str]: ...

    def install(self, progress: ProgressSink) -> None: ...


class RuntimeSupervisor(Protocol):
    def binary_path(self) -> str | None: ...

    def is_managed(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class ConversationRepository(Protocol):
    def ensure(self, conversation_id: str, *, agent: str, title: str) -> Conversation: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list(self, *, limit: int = 100) -> tuple[Conversation, ...]: ...

    def messages(self, conversation_id: str) -> tuple[StoredMessage, ...]: ...

    def append(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str,
        thinking: str | None = None,
        agent: str | None = None,
        interrupted: bool = False,
        stats: dict[str, Any] | None = None,
    ) -> None: ...

    def update(
        self,
        conversation_id: str,
        *,
        system: str | None | UNSET = UNSET,
        title: str | None = None,
        agent: str | None = None,
    ) -> None: ...

    def delete(self, conversation_id: str) -> bool: ...


class DeviceRepository(Protocol):
    def record(
        self,
        device_id: str,
        *,
        readings: Sequence[Reading],
        name: str | None = None,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Device: ...

    def list(self) -> tuple[Device, ...]: ...

    def get(self, device_id: str) -> Device | None: ...

    def history(
        self, device_id: str, *, metric: str | None = None, limit: int = 200
    ) -> tuple[Reading, ...]: ...

    def delete(self, device_id: str) -> bool: ...


class MountRepository(Protocol):
    """Remembers what should be mounted, so a power cut does not lose the setup."""

    def remember(self, name: str) -> None: ...

    def forget(self, name: str) -> None: ...

    def wanted(self) -> tuple[str, ...]: ...


class EventPublisher(Protocol):
    def publish(self, event: Event) -> None: ...


class EventSubscription(Protocol):
    def next(self, timeout: float) -> Event | None: ...

    def close(self) -> None: ...


class EventBus(EventPublisher, Protocol):
    def subscribe(self) -> EventSubscription: ...


type WorkFunction = Callable[[ProgressSink], None]
