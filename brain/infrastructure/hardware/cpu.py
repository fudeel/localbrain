import os
from collections.abc import Callable
from pathlib import Path

from brain.domain.hardware import CpuInfo
from brain.infrastructure.hardware.cgroups import CgroupReader
from brain.infrastructure.hardware.fs import parse_key_values, read_int, read_text

_INTERESTING_FEATURES = frozenset({"avx", "avx2", "avx512f", "avx_vnni", "f16c", "fma", "asimd", "neon"})
_MODEL_KEYS = ("model name", "Model", "Hardware", "cpu model")


class CpuProbe:
    def __init__(
        self,
        root: Path = Path("/"),
        *,
        cgroups: CgroupReader | None = None,
        machine: Callable[[], str] = lambda: os.uname().machine,
        core_count: Callable[[], int | None] = os.process_cpu_count,
    ) -> None:
        self._root = root
        self._cgroups = cgroups or CgroupReader(root)
        self._machine = machine
        self._core_count = core_count

    def read(self) -> CpuInfo:
        fields = parse_key_values(read_text(self._root / "proc/cpuinfo"))
        logical = self._core_count() or 1
        limit = self._cgroups.cpu_limit()
        return CpuInfo(
            model=next((fields[key] for key in _MODEL_KEYS if key in fields), "Unknown CPU"),
            architecture=self._machine(),
            logical_cores=logical,
            effective_cores=min(limit, logical) if limit else float(logical),
            max_frequency_mhz=self._max_frequency(fields),
            features=self._features(fields),
            load_average_1m=self._load_average(),
        )

    def _max_frequency(self, fields: dict[str, str]) -> float | None:
        khz = read_int(self._root / "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        if khz:
            return khz / 1000
        try:
            return float(fields["cpu MHz"])
        except KeyError, ValueError:
            return None

    @staticmethod
    def _features(fields: dict[str, str]) -> tuple[str, ...]:
        flags = fields.get("flags") or fields.get("Features") or ""
        return tuple(sorted(_INTERESTING_FEATURES.intersection(flags.split())))

    @staticmethod
    def _load_average() -> float | None:
        try:
            return os.getloadavg()[0]
        except OSError:
            return None
