"""Decides whether an agent can run on a given machine, and explains why.

New hardware considerations are added as new rules; the evaluator never changes.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Protocol

from brain.domain.hardware import HardwareProfile

MIB = 1024**2
GIB = 1024**3

# Calibrated against a real load: 2.62 GB on disk became 3.31 GB in RAM at a 4096 token context.
_LOAD_FACTOR = 1.15
_MAX_LOAD_OVERHEAD_BYTES = 300 * MIB
_OS_RESERVE_BYTES = 512 * MIB
_DISK_MARGIN_BYTES = 1 * GIB

# Measured about 8 tokens/s at 0.33 GB per core, which still feels interactive.
_COMFORTABLE_GB_PER_CORE = 0.4

_OLLAMA_ARCHITECTURES = frozenset({"x86_64", "amd64", "aarch64", "arm64"})


def estimate_memory_bytes(size_bytes: int) -> int:
    overhead = min(_MAX_LOAD_OVERHEAD_BYTES, size_bytes // 4)
    return round(size_bytes * _LOAD_FACTOR) + overhead


def format_bytes(value: int | float) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024:
            return f"{amount:.0f} {unit}" if unit in ("B", "KB") else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    BLOCKER = 2


class Verdict(StrEnum):
    RUNS_WELL = "runs_well"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Placement(StrEnum):
    GPU = "gpu"
    HYBRID = "hybrid"
    CPU = "cpu"
    NONE = "none"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    severity: Severity
    rule: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Compatibility:
    verdict: Verdict
    available: bool
    placement: Placement
    estimated_memory_bytes: int | None
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Assessment:
    """The question each rule answers: can *this* agent run on *this* machine, right now?"""

    agent_name: str
    is_cloud: bool
    size_bytes: int | None
    is_installed: bool
    mounted_memory_bytes: int | None
    mounted_vram_bytes: int | None
    hardware: HardwareProfile
    estimated_memory_bytes: int | None
    placement: Placement


class CompatibilityRule(Protocol):
    def assess(self, subject: Assessment) -> Iterable[Finding]: ...


class LocalOnlyRule:
    def assess(self, subject: Assessment) -> Iterable[Finding]:
        if subject.is_cloud:
            yield Finding(
                severity=Severity.BLOCKER,
                rule="local_only",
                message="Runs on remote cloud servers, so this local-only brain refuses it.",
            )


class ArchitectureRule:
    def assess(self, subject: Assessment) -> Iterable[Finding]:
        architecture = subject.hardware.cpu.architecture
        if architecture not in _OLLAMA_ARCHITECTURES:
            yield Finding(
                severity=Severity.BLOCKER,
                rule="architecture",
                message=f"Ollama does not support the {architecture} architecture.",
            )


class DiskSpaceRule:
    def assess(self, subject: Assessment) -> Iterable[Finding]:
        if subject.is_installed or subject.size_bytes is None:
            return
        free = subject.hardware.disk.free_bytes
        if free < subject.size_bytes + _DISK_MARGIN_BYTES:
            yield Finding(
                severity=Severity.BLOCKER,
                rule="disk",
                message=(
                    f"Needs {format_bytes(subject.size_bytes)} of disk, "
                    f"but only {format_bytes(free)} is free."
                ),
            )


class MemoryCapacityRule:
    """Can it fit at all, even with nothing else running?"""

    def assess(self, subject: Assessment) -> Iterable[Finding]:
        if subject.mounted_memory_bytes is not None or subject.estimated_memory_bytes is None:
            return
        hardware = subject.hardware
        capacity = hardware.memory.total_bytes - _OS_RESERVE_BYTES + hardware.discrete_vram_bytes
        if subject.estimated_memory_bytes <= capacity:
            return
        source = "container limit" if hardware.memory.limited_by_cgroup else "memory"
        yield Finding(
            severity=Severity.BLOCKER,
            rule="memory",
            message=(
                f"Needs about {format_bytes(subject.estimated_memory_bytes)}, "
                f"but this machine's {source} tops out at {format_bytes(max(capacity, 0))}."
            ),
        )


class FreeMemoryRule:
    """It fits in principle, but does it fit right now?"""

    def assess(self, subject: Assessment) -> Iterable[Finding]:
        needed = subject.estimated_memory_bytes
        if subject.mounted_memory_bytes is not None or needed is None:
            return
        if subject.placement is not Placement.CPU:
            return
        free = subject.hardware.memory.available_bytes
        if needed > free:
            yield Finding(
                severity=Severity.WARNING,
                rule="free_memory",
                message=(
                    f"Needs about {format_bytes(needed)} but only {format_bytes(free)} is free "
                    "right now. Unmount another agent or close other programs first."
                ),
            )


class PlacementRule:
    """Reports where the agent would run. Splitting across GPU and CPU is notably slower."""

    def assess(self, subject: Assessment) -> Iterable[Finding]:
        match subject.placement:
            case Placement.GPU:
                yield Finding(
                    severity=Severity.INFO,
                    rule="placement",
                    message="Fits entirely in GPU memory.",
                )
            case Placement.HYBRID:
                yield Finding(
                    severity=Severity.WARNING,
                    rule="placement",
                    message="Too large for GPU memory alone, so it would split across GPU and CPU.",
                )
            case Placement.CPU if subject.hardware.vram_total_bytes == 0:
                yield Finding(
                    severity=Severity.INFO,
                    rule="placement",
                    message="Runs on the CPU, as no usable GPU was found.",
                )


class CpuThroughputRule:
    """A rough speed check for CPU inference, based on model size per effective core."""

    def assess(self, subject: Assessment) -> Iterable[Finding]:
        if subject.placement is not Placement.CPU or subject.size_bytes is None:
            return
        cpu = subject.hardware.cpu
        comfortable = _COMFORTABLE_GB_PER_CORE
        lacks_vector_units = cpu.architecture in ("x86_64", "amd64") and "avx2" not in cpu.features
        if lacks_vector_units:
            comfortable /= 2
        gb_per_core = (subject.size_bytes / 1e9) / max(cpu.effective_cores, 0.25)
        if gb_per_core > comfortable:
            reason = " without AVX2" if lacks_vector_units else ""
            yield Finding(
                severity=Severity.WARNING,
                rule="cpu_speed",
                message=(
                    f"Expect slow replies on {cpu.effective_cores:g} CPU core(s){reason}: "
                    "fine for background jobs, sluggish for conversation."
                ),
            )


def plan_placement(*, estimated: int | None, hardware: HardwareProfile) -> Placement:
    if estimated is None:
        return Placement.NONE
    if hardware.vram_total_bytes >= estimated:
        return Placement.GPU
    capacity = hardware.memory.total_bytes - _OS_RESERVE_BYTES
    discrete = hardware.discrete_vram_bytes
    if discrete > 0 and discrete + capacity >= estimated:
        return Placement.HYBRID
    return Placement.CPU if capacity >= estimated else Placement.NONE


def _observed_placement(*, memory: int, vram: int) -> Placement:
    if vram <= 0:
        return Placement.CPU
    return Placement.GPU if vram >= memory else Placement.HYBRID


class CompatibilityEvaluator:
    def __init__(self, rules: Sequence[CompatibilityRule]) -> None:
        self._rules = tuple(rules)

    def evaluate(
        self,
        *,
        agent_name: str,
        is_cloud: bool,
        size_bytes: int | None,
        is_installed: bool,
        hardware: HardwareProfile,
        mounted_memory_bytes: int | None = None,
        mounted_vram_bytes: int | None = None,
    ) -> Compatibility:
        estimated = estimate_memory_bytes(size_bytes) if size_bytes is not None else None
        if mounted_memory_bytes is not None:
            # Once loaded, the runtime's own numbers replace the estimate.
            estimated = mounted_memory_bytes
            placement = _observed_placement(
                memory=mounted_memory_bytes, vram=mounted_vram_bytes or 0
            )
        else:
            placement = plan_placement(estimated=estimated, hardware=hardware)

        subject = Assessment(
            agent_name=agent_name,
            is_cloud=is_cloud,
            size_bytes=size_bytes,
            is_installed=is_installed,
            mounted_memory_bytes=mounted_memory_bytes,
            mounted_vram_bytes=mounted_vram_bytes,
            hardware=hardware,
            estimated_memory_bytes=estimated,
            placement=placement,
        )
        findings = tuple(*rule.assess(subject) for rule in self._rules)
        verdict = self._verdict(findings=findings, known_size=estimated is not None)
        return Compatibility(
            verdict=verdict,
            available=verdict is not Verdict.UNAVAILABLE,
            placement=placement,
            estimated_memory_bytes=estimated,
            findings=findings,
        )

    @staticmethod
    def _verdict(*, findings: tuple[Finding, ...], known_size: bool) -> Verdict:
        worst = max((finding.severity for finding in findings), default=Severity.INFO)
        if worst is Severity.BLOCKER:
            return Verdict.UNAVAILABLE
        if not known_size:
            return Verdict.UNKNOWN
        return Verdict.LIMITED if worst is Severity.WARNING else Verdict.RUNS_WELL


def default_rules() -> tuple[CompatibilityRule, ...]:
    return (
        LocalOnlyRule(),
        ArchitectureRule(),
        DiskSpaceRule(),
        MemoryCapacityRule(),
        PlacementRule(),
        CpuThroughputRule(),
        FreeMemoryRule(),
    )
