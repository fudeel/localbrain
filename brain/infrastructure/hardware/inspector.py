import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from brain.domain.hardware import (
    CpuInfo,
    DiskInfo,
    EnvironmentInfo,
    GpuInfo,
    HardwareProfile,
    MemoryInfo,
    Peripheral,
    SensorValue,
)
from brain.infrastructure.hardware.cpu import CpuProbe
from brain.infrastructure.hardware.environment import EnvironmentProbe
from brain.infrastructure.hardware.gpu import DrmProbe, JetsonProbe, NvidiaProbe
from brain.infrastructure.hardware.memory import MemoryProbe
from brain.infrastructure.hardware.peripherals import PeripheralProbe
from brain.infrastructure.hardware.sensors import BatteryProbe, HwmonProbe, ThermalZoneProbe
from brain.infrastructure.hardware.storage import DiskProbe
from brain.ports import HardwareInspector, Probe

_log = logging.getLogger(__name__)


class SystemInspector:
    """Combines single-purpose probes. An optional probe failing never hides the others."""

    def __init__(
        self,
        *,
        cpu: Probe[CpuInfo],
        memory: Probe[MemoryInfo],
        disk: Probe[DiskInfo],
        environment: Probe[EnvironmentInfo],
        gpus: Sequence[Probe[tuple[GpuInfo, ...]]] = (),
        sensors: Sequence[Probe[tuple[SensorValue, ...]]] = (),
        peripherals: Sequence[Probe[tuple[Peripheral, ...]]] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cpu = cpu
        self._memory = memory
        self._disk = disk
        self._environment = environment
        self._gpus = tuple(gpus)
        self._sensors = tuple(sensors)
        self._peripherals = tuple(peripherals)
        self._clock = clock

    def snapshot(self) -> HardwareProfile:
        return HardwareProfile(
            cpu=self._cpu.read(),
            memory=self._memory.read(),
            disk=self._disk.read(),
            environment=self._environment.read(),
            gpus=tuple(*self._read_optional(probe) for probe in self._gpus),
            sensors=tuple(*self._read_optional(probe) for probe in self._sensors),
            peripherals=tuple(*self._read_optional(probe) for probe in self._peripherals),
            taken_at=self._clock(),
        )

    @staticmethod
    def _read_optional[T](probe: Probe[tuple[T, ...]]) -> tuple[T, ...]:
        try:
            return probe.read()
        except Exception:
            _log.warning("Hardware probe %s failed", type(probe).__name__, exc_info=True)
            return ()


class CachingInspector:
    """Keeps listing agents cheap, as probing can spawn processes such as nvidia-smi."""

    def __init__(
        self,
        inner: HardwareInspector,
        *,
        ttl_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: HardwareProfile | None = None
        self._expires = 0.0

    def snapshot(self) -> HardwareProfile:
        with self._lock:
            now = self._monotonic()
            if self._cached is None or now >= self._expires:
                self._cached = self._inner.snapshot()
                self._expires = now + self._ttl
            return self._cached


class UnifiedMemoryAdjuster:
    """Boards like Jetson share RAM with the GPU, so their real 'VRAM' is the RAM size."""

    def __init__(self, inner: HardwareInspector) -> None:
        self._inner = inner

    def snapshot(self) -> HardwareProfile:
        profile = self._inner.snapshot()
        if not any(gpu.unified_memory for gpu in profile.gpus):
            return profile
        gpus = tuple(
            replace(
                gpu,
                vram_total_bytes=profile.memory.total_bytes,
                vram_free_bytes=profile.memory.available_bytes,
            )
            if gpu.unified_memory
            else gpu
            for gpu in profile.gpus
        )
        return replace(profile, gpus=gpus)


def build_inspector(root: Path = Path("/")) -> HardwareInspector:
    system = SystemInspector(
        cpu=CpuProbe(root),
        memory=MemoryProbe(root),
        disk=DiskProbe(),
        environment=EnvironmentProbe(root),
        gpus=(NvidiaProbe(), DrmProbe(root), JetsonProbe(root)),
        sensors=(ThermalZoneProbe(root), HwmonProbe(root), BatteryProbe(root)),
        peripherals=(PeripheralProbe(root),),
    )
    return CachingInspector(UnifiedMemoryAdjuster(system))
