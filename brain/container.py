"""The composition root: the one place that knows which concrete classes are in use."""

from functools import cached_property

from brain.config import Settings, TokenStore
from brain.domain.compatibility import CompatibilityEvaluator, default_rules
from brain.infrastructure.catalog import JsonCatalog
from brain.infrastructure.event_bus import InMemoryEventBus
from brain.infrastructure.hardware.inspector import build_inspector
from brain.infrastructure.ollama.client import OllamaClient
from brain.infrastructure.ollama.installers import (
    GpuArchiveSelector,
    OfficialScriptInstaller,
    RootlessInstaller,
    pick_installer,
)
from brain.infrastructure.ollama.registry import OllamaRegistry
from brain.infrastructure.ollama.supervisor import OllamaSupervisor, is_loopback_url
from brain.infrastructure.storage import (
    Database,
    SqliteConversationRepository,
    SqliteDeviceRepository,
    SqliteMountRepository,
)
from brain.ports import AgentRegistry, HardwareInspector
from brain.services.agents import AgentManager
from brain.services.chat import ChatService
from brain.services.devices import DeviceHub
from brain.services.jobs import JobRunner
from brain.services.runtime import RuntimeManager


class Brain:
    """Builds each collaborator on first use, so `brain hardware` never opens a database."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @cached_property
    def events(self) -> InMemoryEventBus:
        return InMemoryEventBus()

    @cached_property
    def hardware(self) -> HardwareInspector:
        return build_inspector()

    @cached_property
    def ollama(self) -> OllamaClient:
        return OllamaClient(self.settings.ollama_url)

    @cached_property
    def database(self) -> Database:
        return Database(self.settings.data_dir / "brain.db")

    @cached_property
    def jobs(self) -> JobRunner:
        return JobRunner(self.events)

    @cached_property
    def rootless_installer(self) -> RootlessInstaller:
        return RootlessInstaller(self.settings.data_dir / "ollama", extras=GpuArchiveSelector())

    @cached_property
    def supervisor(self) -> OllamaSupervisor:
        return OllamaSupervisor(
            base_url=self.settings.ollama_url,
            rootless_binary=self.rootless_installer.binary,
            log_path=self.settings.data_dir / "ollama.log",
            is_reachable=lambda: self.ollama.version() is not None,
        )

    @cached_property
    def runtime(self) -> RuntimeManager:
        return RuntimeManager(
            url=self.settings.ollama_url,
            is_local=is_loopback_url(self.settings.ollama_url),
            probe=self.ollama,
            supervisor=self.supervisor,
            installers=(OfficialScriptInstaller(), self.rootless_installer),
            pick_installer=pick_installer,
            jobs=self.jobs,
            events=self.events,
        )

    @cached_property
    def registry(self) -> AgentRegistry:
        return OllamaRegistry()

    @cached_property
    def agents(self) -> AgentManager:
        return AgentManager(
            catalog=JsonCatalog(),
            registry=self.registry,
            store=self.ollama,
            loader=self.ollama,
            embeddings=self.ollama,
            hardware=self.hardware,
            evaluator=CompatibilityEvaluator(default_rules()),
            jobs=self.jobs,
            mounts=SqliteMountRepository(self.database),
            events=self.events,
        )

    @cached_property
    def devices(self) -> DeviceHub:
        return DeviceHub(
            repository=SqliteDeviceRepository(
                self.database, retention_days=self.settings.reading_retention_days
            ),
            events=self.events,
            hardware=self.hardware,
        )

    @cached_property
    def chat(self) -> ChatService:
        return ChatService(
            agents=self.agents,
            backend=self.ollama,
            conversations=SqliteConversationRepository(self.database),
            devices=self.devices,
        )

    @cached_property
    def token(self) -> str | None:
        if not self.settings.requires_token:
            return None
        return self.settings.token or TokenStore(self.settings.data_dir).load_or_create()

    def close(self) -> None:
        if "database" in vars(self):
            self.database.close()
