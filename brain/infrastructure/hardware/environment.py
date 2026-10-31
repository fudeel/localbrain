import os
from collections.abc import Mapping
from pathlib import Path

from brain.domain.hardware import Deployment, EnvironmentInfo
from brain.infrastructure.hardware.fs import parse_key_values, read_text

_CONTAINER_MARKERS = ("docker", "containerd", "kubepods", "lxc", "podman")
_MEANINGLESS_BOARD_NAMES = frozenset(
    {"", "to be filled by o.e.m.", "system product name", "default string", "none"}
)


class EnvironmentProbe:
    def __init__(
        self, root: Path = Path("/"), *, environ: Mapping[str, str] = os.environ
    ) -> None:
        self._root = root
        self._environ = environ

    def read(self) -> EnvironmentInfo:
        uname = os.uname()
        release = parse_key_values(read_text(self._root / "etc/os-release"), separator="=")
        return EnvironmentInfo(
            hostname=uname.nodename,
            operating_system=release.get("PRETTY_NAME", uname.sysname).strip('"'),
            kernel=uname.release,
            board=self._board(),
            deployment=self._deployment(),
        )

    def _board(self) -> str | None:
        """Device tree names ARM boards such as Raspberry Pis; DMI names x86 machines."""
        for relative in ("proc/device-tree/model", "sys/class/dmi/id/product_name"):
            text = read_text(self._root / relative)
            if text and (name := text.rstrip("\x00").strip()).lower() not in _MEANINGLESS_BOARD_NAMES:
                return name
        return None

    def _deployment(self) -> Deployment:
        if (
            "KUBERNETES_SERVICE_HOST" in self._environ
            or (self._root / "var/run/secrets/kubernetes.io").exists()
        ):
            return Deployment.KUBERNETES
        if self._is_container():
            return Deployment.CONTAINER
        if self._is_virtual_machine():
            return Deployment.VIRTUAL_MACHINE
        return Deployment.BARE_METAL

    def _is_container(self) -> bool:
        if (self._root / ".dockerenv").exists() or (self._root / "run/.containerenv").exists():
            return True
        init_cgroup = read_text(self._root / "proc/1/cgroup") or ""
        return any(marker in init_cgroup for marker in _CONTAINER_MARKERS)

    def _is_virtual_machine(self) -> bool:
        return "hypervisor" in (read_text(self._root / "proc/cpuinfo") or "")
