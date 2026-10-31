"""An agent is a locally-run model plus the lifecycle this brain manages around it."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from brain.domain.compatibility import Compatibility
from brain.domain.errors import InvalidRequest

_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
_MAX_NAME_LENGTH = 200


class AgentName(str):
    """A validated `model:tag`. Being a `str` keeps it trivially hashable and serializable."""

    __slots__ = ()

    @classmethod
    def parse(cls, raw: str) -> Self:
        candidate = raw.strip()
        if len(candidate) > _MAX_NAME_LENGTH or not _NAME_PATTERN.fullmatch(candidate):
            raise InvalidRequest(
                f"'{raw}' is not a valid agent name.",
                hint="Use the Ollama form, for example qwen3:4b-q4_K_M.",
            )
        if ":" not in candidate:
            candidate = f"{candidate}:latest"
        return cls(candidate)

    @property
    def model(self) -> str:
        return self.rsplit(":", 1)[0]

    @property
    def tag(self) -> str:
        return self.rsplit(":", 1)[1]

    @property
    def is_cloud(self) -> bool:
        """Ollama cloud tags look like local ones but execute on someone else's servers."""
        return self.tag == "cloud" or self.tag.endswith("-cloud")


class Capability(StrEnum):
    COMPLETION = "completion"
    TOOLS = "tools"
    THINKING = "thinking"
    VISION = "vision"
    EMBEDDING = "embedding"
    AUDIO = "audio"


class AgentState(StrEnum):
    AVAILABLE = "available"
    INSTALLING = "installing"
    INSTALLED = "installed"
    MOUNTING = "mounting"
    MOUNTED = "mounted"
    UNMOUNTING = "unmounting"
    UNINSTALLING = "uninstalling"


class AgentSource(StrEnum):
    CATALOG = "catalog"
    LOCAL = "local"
    REGISTRY = "registry"


def parse_capabilities(values: object) -> frozenset[Capability]:
    """Runtimes add capabilities over time, so unknown ones are ignored rather than fatal."""
    known = {capability.value for capability in Capability}
    return frozenset(Capability(value) for value in values or () if value in known)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSpec:
    """What is known about an agent before it is ever downloaded."""

    name: AgentName
    family: str
    summary: str = ""
    capabilities: frozenset[Capability] = frozenset()
    use_cases: tuple[str, ...] = ()
    size_bytes: int | None = None
    parameter_size: str | None = None
    source: AgentSource = AgentSource.CATALOG


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledAgent:
    name: AgentName
    size_bytes: int
    family: str = ""
    digest: str = ""
    modified_at: datetime | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    capabilities: frozenset[Capability] = frozenset()
    remote: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class MountedAgent:
    name: AgentName
    memory_bytes: int
    vram_bytes: int = 0
    expires_at: datetime | None = None
    pinned: bool = False
    context_length: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentActivity:
    """The background job currently moving this agent between states."""

    job_id: str
    kind: str
    message: str = ""
    completed: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Agent:
    """Everything a client needs to show and operate one agent."""

    name: AgentName
    family: str
    summary: str
    state: AgentState
    source: AgentSource
    capabilities: frozenset[Capability]
    use_cases: tuple[str, ...]
    size_bytes: int | None
    parameter_size: str | None
    compatibility: Compatibility
    installed: InstalledAgent | None = None
    mounted: MountedAgent | None = None
    activity: AgentActivity | None = None

    @property
    def can_chat(self) -> bool:
        return Capability.COMPLETION in self.capabilities

    @property
    def is_embedding_only(self) -> bool:
        return Capability.EMBEDDING in self.capabilities and not self.can_chat


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentOverview:
    runtime_online: bool
    agents: tuple[Agent, ...]
