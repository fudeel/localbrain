"""The agent lifecycle: discover, install, mount, unmount, uninstall."""

import logging

from brain.domain.agents import (
    Agent,
    AgentActivity,
    AgentName,
    AgentOverview,
    AgentSource,
    AgentSpec,
    AgentState,
    Capability,
    InstalledAgent,
    MountedAgent,
)
from brain.domain.compatibility import CompatibilityEvaluator, Severity, Verdict
from brain.domain.errors import (
    AgentIncompatible,
    AgentNotInstalled,
    AgentNotMounted,
    CapabilityMissing,
    CloudAgentRefused,
    RuntimeUnavailable,
)
from brain.domain.events import Event
from brain.domain.hardware import HardwareProfile
from brain.domain.jobs import Job, JobKind, JobState
from brain.ports import (
    AgentCatalog,
    AgentRegistry,
    EmbeddingBackend,
    EventPublisher,
    HardwareInspector,
    ModelLoader,
    ModelStore,
    MountRepository,
    ProgressSink,
)
from brain.services.jobs import JobRunner

_log = logging.getLogger(__name__)

_STATE_WHILE_RUNNING = {
    JobKind.INSTALL: AgentState.INSTALLING,
    JobKind.MOUNT: AgentState.MOUNTING,
    JobKind.UNMOUNT: AgentState.UNMOUNTING,
    JobKind.UNINSTALL: AgentState.UNINSTALLING,
}
_STATE_ORDER = {
    AgentState.MOUNTED: 0,
    AgentState.MOUNTING: 1,
    AgentState.UNMOUNTING: 1,
    AgentState.INSTALLED: 2,
    AgentState.INSTALLING: 2,
    AgentState.UNINSTALLING: 2,
    AgentState.AVAILABLE: 3,
}
_VERDICT_ORDER = {
    Verdict.RUNS_WELL: 0,
    Verdict.LIMITED: 1,
    Verdict.UNKNOWN: 2,
    Verdict.UNAVAILABLE: 3,
}


def _friendly_pull_status(status: str) -> str:
    if status == "pulling manifest":
        return "Preparing"
    if status.startswith("pulling "):
        return "Downloading"
    if status.startswith("verifying"):
        return "Verifying"
    if status.startswith("writing"):
        return "Finishing"
    return "Done" if status == "success" else status


def _refuse_cloud(name: AgentName) -> None:
    if name.is_cloud:
        raise CloudAgentRefused(
            f"{name} runs on remote cloud servers, and this brain only runs agents locally."
        )


class AgentManager:
    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        registry: AgentRegistry,
        store: ModelStore,
        loader: ModelLoader,
        embeddings: EmbeddingBackend,
        hardware: HardwareInspector,
        evaluator: CompatibilityEvaluator,
        jobs: JobRunner,
        mounts: MountRepository,
        events: EventPublisher,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._store = store
        self._loader = loader
        self._embeddings = embeddings
        self._hardware = hardware
        self._evaluator = evaluator
        self._jobs = jobs
        self._mounts = mounts
        self._events = events

    def overview(self) -> AgentOverview:
        hardware = self._hardware.snapshot()
        online, installed, mounted = self._runtime_state()
        specs: dict[AgentName, AgentSpec | None] = {spec.name: spec for spec in self._catalog.all()}
        for name in installed:
            specs.setdefault(name, None)
        agents = [
            self._build(
                name, spec=spec, hardware=hardware, installed=installed, mounted=mounted
            )
            for name, spec in specs.items()
        ]
        agents.sort(
            key=lambda agent: (
                _STATE_ORDER[agent.state],
                _VERDICT_ORDER[agent.compatibility.verdict],
                agent.size_bytes if agent.size_bytes is not None else 0,
                agent.name,
            )
        )
        return AgentOverview(runtime_online=online, agents=tuple(agents))

    def get(self, raw_name: str) -> Agent:
        """Any agent can be asked about: known ones instantly, others via the registry."""
        name = AgentName.parse(raw_name)
        hardware = self._hardware.snapshot()
        _, installed, mounted = self._runtime_state()
        spec = self._catalog.find(name)
        if spec is None and name not in installed and not name.is_cloud:
            spec = self._registry.lookup(name)
        return self._build(name, spec=spec, hardware=hardware, installed=installed, mounted=mounted)

    def install(self, raw_name: str, *, force: bool = False) -> Job:
        agent = self.get(raw_name)
        _refuse_cloud(agent.name)
        self._require_runtime()
        if not agent.compatibility.available and not force:
            raise self._incompatible(agent)
        return self._jobs.submit(
            kind=JobKind.INSTALL,
            target=agent.name,
            work=lambda progress: self._pull(agent.name, progress),
            on_finished=self._announce_agent_change,
        )

    def mount(self, raw_name: str, *, force: bool = False) -> Job:
        agent = self.get(raw_name)
        _refuse_cloud(agent.name)
        self._require_installed(agent)
        if agent.installed is not None and agent.installed.remote:
            raise CloudAgentRefused(f"{agent.name} is a remote cloud model.")
        if not agent.compatibility.available and not force:
            raise self._incompatible(agent)
        return self._jobs.submit(
            kind=JobKind.MOUNT,
            target=agent.name,
            work=lambda progress: self._load(agent, progress),
            on_finished=self._announce_agent_change,
        )

    def unmount(self, raw_name: str) -> Job:
        agent = self.get(raw_name)
        self._require_runtime()
        return self._jobs.submit(
            kind=JobKind.UNMOUNT,
            target=agent.name,
            work=lambda progress: self._unload(agent, progress),
            on_finished=self._announce_agent_change,
        )

    def uninstall(self, raw_name: str) -> Job:
        agent = self.get(raw_name)
        self._require_installed(agent)
        return self._jobs.submit(
            kind=JobKind.UNINSTALL,
            target=agent.name,
            work=lambda progress: self._delete(agent, progress),
            on_finished=self._announce_agent_change,
        )

    def wait(self, job: Job, timeout: float | None = None) -> Job:
        return self._jobs.wait(job.id, timeout)

    def require_mounted(self, raw_name: str, *, auto_mount: bool = False) -> Agent:
        """Mounting claims scarce memory, so it never happens by surprise unless asked for."""
        agent = self.get(raw_name)
        _refuse_cloud(agent.name)
        self._require_installed(agent)
        if agent.installed is not None and agent.installed.remote:
            raise CloudAgentRefused(f"{agent.name} is a remote cloud model.")
        if agent.mounted is not None:
            return agent
        if not auto_mount:
            raise AgentNotMounted(
                f"{agent.name} is installed but not mounted.",
                hint="Mount it first, or send auto_mount: true.",
            )
        finished = self.wait(self.mount(agent.name))
        if finished.state is not JobState.SUCCEEDED:
            raise AgentNotMounted(finished.error or f"Could not mount {agent.name}.")
        return self.get(agent.name)

    def embed(self, raw_name: str, inputs: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        agent = self.require_mounted(raw_name)
        if Capability.EMBEDDING not in agent.capabilities:
            raise CapabilityMissing(f"{agent.name} cannot produce embeddings.")
        return self._embeddings.embed(agent.name, inputs)

    def restore_mounts(self) -> tuple[Job, ...]:
        """Puts back what was mounted before the last shutdown or power cut."""
        jobs = []
        for name in self._mounts.wanted():
            try:
                agent = self.get(name)
                if agent.installed is None:
                    self._mounts.forget(name)
                elif agent.mounted is None:
                    _log.info("Restoring mount of %s", name)
                    jobs.append(self.mount(name))
            except Exception:
                _log.warning("Could not restore mount of %s", name, exc_info=True)
        return tuple(jobs)

    def _runtime_state(
        self,
    ) -> tuple[bool, dict[AgentName, InstalledAgent], dict[AgentName, MountedAgent]]:
        try:
            installed = {item.name: item for item in self._store.list_installed()}
            mounted = {item.name: item for item in self._loader.list_loaded()}
        except RuntimeUnavailable:
            return False, {}, {}
        return True, installed, mounted

    def _require_runtime(self) -> None:
        self._store.list_installed()

    def _require_installed(self, agent: Agent) -> None:
        self._require_runtime()
        if agent.installed is None:
            raise AgentNotInstalled(f"{agent.name} is not installed.", hint="Install it first.")

    @staticmethod
    def _incompatible(agent: Agent) -> AgentIncompatible:
        blockers = [
            finding.message
            for finding in agent.compatibility.findings
            if finding.severity is Severity.BLOCKER
        ]
        return AgentIncompatible(
            f"{agent.name} cannot run on this machine. {' '.join(blockers)}",
            hint="Send force: true to try anyway.",
        )

    def _build(
        self,
        name: AgentName,
        *,
        spec: AgentSpec | None,
        hardware: HardwareProfile,
        installed: dict[AgentName, InstalledAgent],
        mounted: dict[AgentName, MountedAgent],
    ) -> Agent:
        local = installed.get(name)
        loaded = mounted.get(name)
        size = local.size_bytes if local else (spec.size_bytes if spec else None)
        activity = self._activity_for(name)
        return Agent(
            name=name,
            family=(spec.family if spec else None) or (local.family if local else name.model),
            summary=spec.summary if spec else "",
            state=self._state(local=local, loaded=loaded, activity=activity),
            source=spec.source if spec else AgentSource.LOCAL,
            # What Ollama reports about an installed model beats anything the catalog assumed.
            capabilities=(
                local.capabilities
                if local and local.capabilities
                else (spec.capabilities if spec else frozenset())
            ),
            use_cases=spec.use_cases if spec else (),
            size_bytes=size,
            parameter_size=(local.parameter_size if local else None)
            or (spec.parameter_size if spec else None),
            compatibility=self._evaluator.evaluate(
                agent_name=name,
                is_cloud=name.is_cloud or bool(local and local.remote),
                size_bytes=size,
                is_installed=local is not None,
                hardware=hardware,
                mounted_memory_bytes=loaded.memory_bytes if loaded else None,
                mounted_vram_bytes=loaded.vram_bytes if loaded else None,
            ),
            installed=local,
            mounted=loaded,
            activity=activity,
        )

    def _activity_for(self, name: AgentName) -> AgentActivity | None:
        for job in self._jobs.active():
            if job.target == name:
                return AgentActivity(
                    job_id=job.id,
                    kind=job.kind.value,
                    message=job.message,
                    completed=job.completed,
                    total=job.total,
                )
        return None

    @staticmethod
    def _state(
        *,
        local: InstalledAgent | None,
        loaded: MountedAgent | None,
        activity: AgentActivity | None,
    ) -> AgentState:
        if activity is not None:
            state = _STATE_WHILE_RUNNING.get(JobKind(activity.kind))
            if state is not None:
                return state
        if loaded is not None:
            return AgentState.MOUNTED
        return AgentState.INSTALLED if local is not None else AgentState.AVAILABLE

    def _pull(self, name: AgentName, progress: ProgressSink) -> None:
        for update in self._store.pull(name):
            progress.raise_if_cancelled()
            progress.report(
                _friendly_pull_status(update.status),
                completed=update.completed,
                total=update.total,
            )

    def _load(self, agent: Agent, progress: ProgressSink) -> None:
        progress.report("Loading into memory")
        self._loader.load(agent.name, embedding=agent.is_embedding_only)
        self._mounts.remember(agent.name)

    def _unload(self, agent: Agent, progress: ProgressSink) -> None:
        progress.report("Releasing memory")
        self._mounts.forget(agent.name)
        if agent.mounted is not None:
            self._loader.unload(agent.name, embedding=agent.is_embedding_only)

    def _delete(self, agent: Agent, progress: ProgressSink) -> None:
        self._mounts.forget(agent.name)
        if agent.mounted is not None:
            progress.report("Releasing memory")
            self._loader.unload(agent.name, embedding=agent.is_embedding_only)
        progress.report("Deleting files")
        self._store.delete(agent.name)

    def _announce_agent_change(self, job: Job) -> None:
        self._events.publish(
            Event(
                type="agent.changed",
                data=frozendict(name=job.target, kind=job.kind.value, state=job.state.value),
            )
        )
