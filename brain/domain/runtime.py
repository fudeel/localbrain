"""The state of the model runtime (Ollama) on this machine."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallerOption:
    method: str
    label: str
    usable: bool
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeStatus:
    url: str
    is_local: bool
    reachable: bool
    version: str | None
    binary_path: str | None
    managed: bool
    installers: tuple[InstallerOption, ...] = ()
    manual_command: str = "curl -fsSL https://ollama.com/install.sh | sh"

    @property
    def installed(self) -> bool:
        return self.reachable or self.binary_path is not None
