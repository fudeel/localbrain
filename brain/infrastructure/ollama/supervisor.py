"""Finds the Ollama binary and gets its server running."""

lazy import shutil
lazy import subprocess

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from brain.domain.errors import RuntimeUnavailable

_log = logging.getLogger(__name__)
_START_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.25
_STOP_TIMEOUT_SECONDS = 10.0


def is_loopback_url(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


class OllamaSupervisor:
    """Prefers an existing systemd service, and otherwise runs Ollama as a child process."""

    def __init__(
        self,
        *,
        base_url: str,
        rootless_binary: Path,
        log_path: Path,
        is_reachable: Callable[[], bool],
        environ: Mapping[str, str] = os.environ,
        start_timeout: float = _START_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url
        self._rootless_binary = rootless_binary
        self._log_path = log_path
        self._is_reachable = is_reachable
        self._environ = environ
        self._start_timeout = start_timeout
        self._lock = threading.Lock()
        self._child: subprocess.Popen[bytes] | None = None

    def binary_path(self) -> str | None:
        if found := shutil.which("ollama"):
            return found
        return str(self._rootless_binary) if self._rootless_binary.is_file() else None

    def is_managed(self) -> bool:
        with self._lock:
            return self._child is not None and self._child.poll() is None

    def start(self) -> None:
        if self._is_reachable():
            return
        if not is_loopback_url(self._base_url):
            raise RuntimeUnavailable(
                f"Ollama at {self._base_url} is not answering, and it runs on another machine."
            )
        binary = self.binary_path()
        if binary is None:
            raise RuntimeUnavailable("Ollama is not installed.", hint="Install it first.")
        with self._lock:
            if not self._start_system_service():
                self._spawn(binary)
        self._wait_until_reachable()

    def stop(self) -> None:
        """Stops only the Ollama this process started. A system service is left alone."""
        with self._lock:
            child, self._child = self._child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def _start_system_service(self) -> bool:
        if shutil.which("systemctl") is None:
            return False
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        if prefix and shutil.which("sudo") is None:
            return False
        try:
            completed = subprocess.run(
                [*prefix, "systemctl", "start", "ollama"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return False
        return completed.returncode == 0

    def _spawn(self, binary: str) -> None:
        if self._child is not None and self._child.poll() is None:
            return
        parts = urlsplit(self._base_url)
        environment = {
            **self._environ,
            "OLLAMA_HOST": f"{parts.hostname or '127.0.0.1'}:{parts.port or 11434}",
            # This brain is local-only, and its own Ollama is configured to match.
            "OLLAMA_NO_CLOUD": "1",
        }
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("ab") as log:
            self._child = subprocess.Popen(
                [binary, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
        _log.info("Started Ollama as a child process (pid %s)", self._child.pid)

    def _wait_until_reachable(self) -> None:
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._is_reachable():
                return
            child = self._child
            if child is not None and child.poll() is not None:
                raise RuntimeUnavailable(
                    f"Ollama exited with code {child.returncode} while starting.",
                    hint=f"See {self._log_path} for details.",
                )
            time.sleep(_POLL_SECONDS)
        raise RuntimeUnavailable(
            f"Ollama did not start within {self._start_timeout:.0f} seconds.",
            hint=f"See {self._log_path} for details.",
        )
