"""Makes sure a model runtime exists on this machine and is running."""

import logging
from collections.abc import Callable, Sequence

from brain.domain.errors import Conflict, RuntimeUnavailable
from brain.domain.events import Event
from brain.domain.jobs import Job, JobKind
from brain.domain.runtime import InstallerOption, RuntimeStatus
from brain.ports import (
    EventPublisher,
    ProgressSink,
    RuntimeInstaller,
    RuntimeProbe,
    RuntimeSupervisor,
)
from brain.services.jobs import JobRunner

_log = logging.getLogger(__name__)
_RUNTIME_TARGET = "ollama"

type InstallerPicker = Callable[[Sequence[RuntimeInstaller], str | None], RuntimeInstaller]


class RuntimeManager:
    def __init__(
        self,
        *,
        url: str,
        is_local: bool,
        probe: RuntimeProbe,
        supervisor: RuntimeSupervisor,
        installers: Sequence[RuntimeInstaller],
        pick_installer: InstallerPicker,
        jobs: JobRunner,
        events: EventPublisher,
    ) -> None:
        self._url = url
        self._is_local = is_local
        self._probe = probe
        self._supervisor = supervisor
        self._installers = tuple(installers)
        self._pick_installer = pick_installer
        self._jobs = jobs
        self._events = events

    def status(self) -> RuntimeStatus:
        version = self._probe.version()
        binary = self._supervisor.binary_path() if self._is_local else None
        # Install options are only worked out when they matter, since checking them is not free.
        needs_install = self._is_local and version is None and binary is None
        return RuntimeStatus(
            url=self._url,
            is_local=self._is_local,
            reachable=version is not None,
            version=version,
            binary_path=binary,
            managed=self._supervisor.is_managed(),
            installers=self._installer_options() if needs_install else (),
        )

    def install(self, *, method: str | None = None) -> Job:
        self._require_local("installed")
        if self._probe.version() is not None:
            raise Conflict("Ollama is already installed and running.")
        installer = self._pick_installer(self._installers, method)
        return self._jobs.submit(
            kind=JobKind.RUNTIME_INSTALL,
            target=_RUNTIME_TARGET,
            work=lambda progress: self._install(installer, progress),
            on_finished=self._announce,
        )

    def start(self) -> Job:
        return self._jobs.submit(
            kind=JobKind.RUNTIME_START,
            target=_RUNTIME_TARGET,
            work=self._start,
            on_finished=self._announce,
        )

    def ensure_ready(self, *, allow_install: bool) -> Job | None:
        """Called at startup: start what is installed, and install what is missing if allowed."""
        if not self._is_local or self._probe.version() is not None:
            return None
        if self._supervisor.binary_path() is not None:
            return self.start()
        if not allow_install:
            _log.info("Ollama is not installed. Install it from the dashboard or the CLI.")
            return None
        return self.install()

    def shutdown(self) -> None:
        self._supervisor.stop()

    def wait(self, job: Job, timeout: float | None = None) -> Job:
        return self._jobs.wait(job.id, timeout)

    def _installer_options(self) -> tuple[InstallerOption, ...]:
        return tuple(
            InstallerOption(
                method=installer.method, label=installer.label, usable=usable, reason=reason
            )
            for installer in self._installers
            for usable, reason in (installer.availability(),)
        )

    def _install(self, installer: RuntimeInstaller, progress: ProgressSink) -> None:
        installer.install(progress)
        self._start(progress)

    def _start(self, progress: ProgressSink) -> None:
        progress.report("Starting Ollama", completed=None, total=None)
        self._supervisor.start()

    def _announce(self, job: Job) -> None:
        self._events.publish(
            Event(type="runtime.changed", data=frozendict(kind=job.kind.value, state=job.state.value))
        )

    def _require_local(self, action: str) -> None:
        if not self._is_local:
            raise RuntimeUnavailable(
                f"Ollama at {self._url} runs on another machine, so it cannot be {action} here."
            )
