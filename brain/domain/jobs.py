"""Long-running work such as downloads and model loads, observed through snapshots."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobKind(StrEnum):
    INSTALL = "install"
    MOUNT = "mount"
    UNMOUNT = "unmount"
    UNINSTALL = "uninstall"
    RUNTIME_INSTALL = "runtime_install"
    RUNTIME_START = "runtime_start"


class JobState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class Job:
    id: str
    kind: JobKind
    target: str
    state: JobState
    started_at: datetime
    message: str = ""
    completed: int | None = None
    total: int | None = None
    error: str | None = None
    finished_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.state is JobState.RUNNING
