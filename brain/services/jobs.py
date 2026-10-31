"""Runs slow work in the background and keeps everyone informed about it."""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime

from brain.domain.errors import BrainError, Conflict, JobCancelled, NotFound
from brain.domain.events import Event
from brain.domain.jobs import Job, JobKind, JobState
from brain.ports import EventPublisher, WorkFunction

_log = logging.getLogger(__name__)
_RETAINED_FINISHED_JOBS = 100
# A download reports many times a second, which no human or dashboard needs to see.
_MIN_EVENT_INTERVAL_SECONDS = 0.25


class _JobProgress:
    """The handle a running job uses to report progress. It implements ProgressSink."""

    def __init__(self, runner: JobRunner, job_id: str) -> None:
        self._runner = runner
        self._job_id = job_id
        self._cancelled = threading.Event()
        self._last_event = 0.0

    def report(
        self,
        message: str | None = None,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        now = time.monotonic()
        changed_message = self._runner.update(
            self._job_id, message=message, completed=completed, total=total
        )
        if changed_message or now - self._last_event >= _MIN_EVENT_INTERVAL_SECONDS:
            self._last_event = now
            self._runner.announce(self._job_id)

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise JobCancelled("Cancelled.")

    def cancel(self) -> None:
        self._cancelled.set()


class JobRunner:
    def __init__(self, events: EventPublisher) -> None:
        self._events = events
        self._changed = threading.Condition()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._handles: dict[str, _JobProgress] = {}

    def submit(
        self,
        *,
        kind: JobKind,
        target: str,
        work: WorkFunction,
        on_finished: Callable[[Job], None] | None = None,
    ) -> Job:
        """Repeating a request returns the job already running rather than starting a second one."""
        with self._changed:
            for job in self._jobs.values():
                if job.is_active and job.target == target:
                    if job.kind is kind:
                        return job
                    raise Conflict(f"{target} is busy: a {job.kind.value} is in progress.")
            job = Job(
                id=str(uuid.uuid7()),
                kind=kind,
                target=target,
                state=JobState.RUNNING,
                started_at=datetime.now(UTC),
            )
            self._jobs[job.id] = job
            handle = _JobProgress(self, job.id)
            self._handles[job.id] = handle
            self._forget_old_jobs()

        self.announce(job.id)
        threading.Thread(
            target=self._run,
            args=(job.id, work, handle, on_finished),
            name=f"job-{kind.value}",
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._changed:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(f"There is no job {job_id}.")
        return job

    def all(self) -> tuple[Job, ...]:
        with self._changed:
            return tuple(reversed(self._jobs.values()))

    def active(self) -> tuple[Job, ...]:
        with self._changed:
            return tuple(job for job in self._jobs.values() if job.is_active)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._changed:
            handle = self._handles.get(job_id)
        if handle is not None:
            handle.cancel()
        return job

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    raise NotFound(f"There is no job {job_id}.")
                if not job.is_active:
                    return job
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return job
                self._changed.wait(remaining)

    def update(
        self,
        job_id: str,
        *,
        message: str | None = None,
        completed: int | None = None,
        total: int | None = None,
    ) -> bool:
        with self._changed:
            job = self._jobs[job_id]
            changed_message = message is not None and message != job.message
            self._jobs[job_id] = replace(
                job,
                message=message if message is not None else job.message,
                completed=completed,
                total=total,
            )
            self._changed.notify_all()
        return changed_message

    def announce(self, job_id: str) -> None:
        with self._changed:
            job = self._jobs.get(job_id)
        if job is not None:
            self._events.publish(Event(type="job.updated", data=frozendict(_job_payload(job))))

    def _run(
        self,
        job_id: str,
        work: WorkFunction,
        handle: _JobProgress,
        on_finished: Callable[[Job], None] | None,
    ) -> None:
        state, error = JobState.SUCCEEDED, None
        try:
            work(handle)
        except JobCancelled:
            state = JobState.CANCELLED
        except BrainError as failure:
            state, error = JobState.FAILED, failure.message
        except Exception as failure:
            _log.exception("Job %s crashed", job_id)
            state, error = JobState.FAILED, f"Unexpected error: {failure}"

        with self._changed:
            finished = replace(
                self._jobs[job_id], state=state, error=error, finished_at=datetime.now(UTC)
            )
            self._jobs[job_id] = finished
            self._handles.pop(job_id, None)
            self._changed.notify_all()
        self.announce(job_id)
        if on_finished is not None:
            on_finished(finished)

    def _forget_old_jobs(self) -> None:
        finished = [job_id for job_id, job in self._jobs.items() if not job.is_active]
        for job_id in finished[: max(len(finished) - _RETAINED_FINISHED_JOBS, 0)]:
            del self._jobs[job_id]


def _job_payload(job: Job) -> dict:
    payload = asdict(job)
    payload["kind"] = job.kind.value
    payload["state"] = job.state.value
    payload["started_at"] = job.started_at.isoformat()
    payload["finished_at"] = job.finished_at.isoformat() if job.finished_at else None
    return payload
