from pathlib import Path

from brain.domain.hardware import MemoryInfo
from brain.infrastructure.hardware.cgroups import CgroupReader
from brain.infrastructure.hardware.fs import parse_key_values, read_text


def _kib(fields: dict[str, str], key: str) -> int:
    return int(fields.get(key, "0 kB").split()[0]) * 1024


class MemoryProbe:
    def __init__(self, root: Path = Path("/"), *, cgroups: CgroupReader | None = None) -> None:
        self._root = root
        self._cgroups = cgroups or CgroupReader(root)

    def read(self) -> MemoryInfo:
        fields = parse_key_values(read_text(self._root / "proc/meminfo"))
        host_total = _kib(fields, "MemTotal")
        host_available = _kib(fields, "MemAvailable")

        limit = self._cgroups.memory_limit()
        if limit is None or limit.limit_bytes >= host_total:
            return MemoryInfo(
                total_bytes=host_total,
                available_bytes=host_available,
                host_total_bytes=host_total,
            )
        room_in_limit = max(limit.limit_bytes - limit.used_bytes, 0)
        return MemoryInfo(
            total_bytes=limit.limit_bytes,
            available_bytes=min(room_in_limit, host_available),
            host_total_bytes=host_total,
            limited_by_cgroup=True,
        )
