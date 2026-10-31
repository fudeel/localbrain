"""What a machine can do, as immutable values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Deployment(StrEnum):
    BARE_METAL = "bare_metal"
    VIRTUAL_MACHINE = "virtual_machine"
    CONTAINER = "container"
    KUBERNETES = "kubernetes"


@dataclass(frozen=True, slots=True, kw_only=True)
class CpuInfo:
    model: str
    architecture: str
    logical_cores: int
    effective_cores: float
    max_frequency_mhz: float | None = None
    features: tuple[str, ...] = ()
    load_average_1m: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryInfo:
    """`total_bytes` and `available_bytes` already respect any container limit."""

    total_bytes: int
    available_bytes: int
    host_total_bytes: int
    limited_by_cgroup: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class GpuInfo:
    vendor: str
    name: str
    vram_total_bytes: int
    vram_free_bytes: int | None = None
    supported: bool = True
    unified_memory: bool = False
    temperature_c: float | None = None
    utilization_percent: float | None = None
    driver: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DiskInfo:
    path: str
    total_bytes: int
    free_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorValue:
    source: str
    label: str
    kind: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Peripheral:
    kind: str
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentInfo:
    hostname: str
    operating_system: str
    kernel: str
    board: str | None = None
    deployment: Deployment = Deployment.BARE_METAL


@dataclass(frozen=True, slots=True, kw_only=True)
class HardwareProfile:
    cpu: CpuInfo
    memory: MemoryInfo
    disk: DiskInfo
    environment: EnvironmentInfo
    taken_at: datetime
    gpus: tuple[GpuInfo, ...] = ()
    sensors: tuple[SensorValue, ...] = ()
    peripherals: tuple[Peripheral, ...] = ()

    @property
    def inference_gpus(self) -> tuple[GpuInfo, ...]:
        return tuple(gpu for gpu in self.gpus if gpu.supported)

    @property
    def vram_total_bytes(self) -> int:
        return sum(gpu.vram_total_bytes for gpu in self.inference_gpus)

    @property
    def discrete_vram_bytes(self) -> int:
        """VRAM on top of system RAM. Unified memory must not be counted twice."""
        return sum(gpu.vram_total_bytes for gpu in self.inference_gpus if not gpu.unified_memory)
