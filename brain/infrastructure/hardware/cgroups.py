"""Container and systemd limits.

Inside Docker or Kubernetes, /proc/meminfo reports the *host's* memory. A pod limited to 2 GiB on
a 256 GiB server would otherwise be offered agents that can only get killed by the OOM reaper.
"""

from dataclasses import dataclass
from pathlib import Path

from brain.infrastructure.hardware.fs import parse_key_values, read_int, read_text

# cgroup v1 reports "no limit" as a huge page-aligned number instead of "max".
_UNLIMITED_THRESHOLD = 1 << 60


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryLimit:
    limit_bytes: int
    used_bytes: int


class CgroupReader:
    def __init__(self, root: Path = Path("/")) -> None:
        self._mount = root / "sys/fs/cgroup"
        self._proc_cgroup = root / "proc/self/cgroup"

    def memory_limit(self) -> MemoryLimit | None:
        return self._v2_memory_limit() or self._v1_memory_limit()

    def cpu_limit(self) -> float | None:
        return self._v2_cpu_limit() or self._v1_cpu_limit()

    def _v2_chain(self) -> list[Path]:
        """Limits are inherited, so the tightest may sit on any ancestor of our own cgroup."""
        for line in (read_text(self._proc_cgroup) or "").splitlines():
            hierarchy, _, relative = line.partition("::")
            if hierarchy == "0":
                current = self._mount / relative.lstrip("/")
                return [current, *current.parents[: len(Path(relative.lstrip("/")).parts)]]
        return [self._mount]

    def _v2_memory_limit(self) -> MemoryLimit | None:
        best: MemoryLimit | None = None
        for directory in self._v2_chain():
            raw = read_text(directory / "memory.max")
            if raw is None or raw == "max":
                continue
            try:
                limit = int(raw)
            except ValueError:
                continue
            if best is None or limit < best.limit_bytes:
                used = self._without_reclaimable_cache(
                    usage=read_int(directory / "memory.current") or 0,
                    stat=read_text(directory / "memory.stat"),
                    key="inactive_file",
                )
                best = MemoryLimit(limit_bytes=limit, used_bytes=used)
        return best

    def _v1_memory_limit(self) -> MemoryLimit | None:
        directory = self._mount / "memory"
        limit = read_int(directory / "memory.limit_in_bytes")
        if limit is None or limit >= _UNLIMITED_THRESHOLD:
            return None
        used = self._without_reclaimable_cache(
            usage=read_int(directory / "memory.usage_in_bytes") or 0,
            stat=read_text(directory / "memory.stat"),
            key="total_inactive_file",
        )
        return MemoryLimit(limit_bytes=limit, used_bytes=used)

    @staticmethod
    def _without_reclaimable_cache(*, usage: int, stat: str | None, key: str) -> int:
        reclaimable = parse_key_values(stat, separator=" ").get(key, "0")
        return max(usage - int(reclaimable), 0)

    def _v2_cpu_limit(self) -> float | None:
        best: float | None = None
        for directory in self._v2_chain():
            quota, _, period = (read_text(directory / "cpu.max") or "").partition(" ")
            if not quota or quota == "max":
                continue
            cores = int(quota) / int(period or 100_000)
            best = cores if best is None else min(best, cores)
        return best

    def _v1_cpu_limit(self) -> float | None:
        directory = self._mount / "cpu"
        quota = read_int(directory / "cpu.cfs_quota_us")
        period = read_int(directory / "cpu.cfs_period_us")
        if not quota or quota < 0 or not period:
            return None
        return quota / period
