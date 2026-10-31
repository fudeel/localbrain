lazy import shutil

import os
from collections.abc import Mapping
from pathlib import Path

from brain.domain.hardware import DiskInfo


class ModelDirectoryLocator:
    """Agents consume disk where Ollama keeps models, which may not be the root filesystem."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] = os.environ,
        home: Path | None = None,
        service_home: Path = Path("/usr/share/ollama"),
    ) -> None:
        self._environ = environ
        self._home = home or Path.home()
        self._service_home = service_home

    def locate(self) -> Path:
        if configured := self._environ.get("OLLAMA_MODELS"):
            return Path(configured)
        system_store = self._service_home / ".ollama/models"
        if self._service_home.exists():
            return system_store
        return self._home / ".ollama/models"


class DiskProbe:
    def __init__(self, locator: ModelDirectoryLocator | None = None) -> None:
        self._locator = locator or ModelDirectoryLocator()

    def read(self) -> DiskInfo:
        target = self._locator.locate()
        usage, measured = self._usage_of_nearest_existing(target)
        return DiskInfo(path=str(measured), total_bytes=usage.total, free_bytes=usage.free)

    @staticmethod
    def _usage_of_nearest_existing(target: Path):
        for candidate in (target, *target.parents):
            try:
                return shutil.disk_usage(candidate), candidate
            except OSError:
                continue
        return shutil.disk_usage("/"), Path("/")
