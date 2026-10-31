"""GPU discovery. Adding a vendor means adding a probe; nothing else changes."""

lazy import shutil
lazy import subprocess

from collections.abc import Callable, Sequence
from pathlib import Path

from brain.domain.hardware import GpuInfo
from brain.infrastructure.hardware.fs import read_int, read_text

MIB = 1024**2

type CommandRunner = Callable[[Sequence[str]], str | None]


def run_command(argv: Sequence[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=5, check=False, encoding="utf-8"
        )
    except OSError, subprocess.SubprocessError:
        return None
    return completed.stdout if completed.returncode == 0 else None


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None  # nvidia-smi prints "[N/A]" for anything a card does not report.


class NvidiaProbe:
    _QUERY = "name,memory.total,memory.free,temperature.gpu,utilization.gpu,driver_version"

    def __init__(self, run: CommandRunner = run_command) -> None:
        self._run = run

    def read(self) -> tuple[GpuInfo, ...]:
        output = self._run(
            ["nvidia-smi", f"--query-gpu={self._QUERY}", "--format=csv,noheader,nounits"]
        )
        if not output:
            return ()
        return tuple(
            gpu for line in output.splitlines() if line.strip() and (gpu := self._parse(line))
        )

    @staticmethod
    def _parse(line: str) -> GpuInfo | None:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            return None
        name, total, free, temperature, utilization, driver = parts[:6]
        total_mib = _number(total)
        if total_mib is None:
            return None
        free_mib = _number(free)
        return GpuInfo(
            vendor="NVIDIA",
            name=name,
            vram_total_bytes=int(total_mib * MIB),
            vram_free_bytes=int(free_mib * MIB) if free_mib is not None else None,
            temperature_c=_number(temperature),
            utilization_percent=_number(utilization),
            driver=driver,
        )


class DrmProbe:
    """Finds AMD and Intel cards through the kernel's DRM interface in /sys."""

    def __init__(self, root: Path = Path("/")) -> None:
        self._root = root

    def read(self) -> tuple[GpuInfo, ...]:
        drm = self._root / "sys/class/drm"
        if not drm.is_dir():
            return ()
        cards = sorted(path for path in drm.glob("card*") if path.name[4:].isdigit())
        return tuple(gpu for card in cards if (gpu := self._read_card(card / "device")))

    def _read_card(self, device: Path) -> GpuInfo | None:
        match read_text(device / "vendor"):
            case "0x1002":
                return self._amd(device)
            case "0x8086":
                return GpuInfo(
                    vendor="Intel",
                    name=self._name(device, "Intel GPU"),
                    vram_total_bytes=read_int(device / "mem_info_vram_total") or 0,
                    supported=False,
                    note="Ollama has no direct Intel backend; Vulkan needs manual setup.",
                )
            case _:
                return None

    def _amd(self, device: Path) -> GpuInfo:
        total = read_int(device / "mem_info_vram_total") or 0
        used = read_int(device / "mem_info_vram_used")
        rocm_ready = (self._root / "dev/kfd").exists()
        return GpuInfo(
            vendor="AMD",
            name=self._name(device, "AMD GPU"),
            vram_total_bytes=total,
            vram_free_bytes=total - used if used is not None else None,
            supported=rocm_ready and total > 0,
            temperature_c=self._temperature(device),
            note=None if rocm_ready else "The ROCm driver (/dev/kfd) was not found.",
        )

    @staticmethod
    def _name(device: Path, fallback: str) -> str:
        product = read_text(device / "product_name")
        if product:
            return product
        identifier = read_text(device / "device")
        return f"{fallback} ({identifier})" if identifier else fallback

    @staticmethod
    def _temperature(device: Path) -> float | None:
        for sensor in device.glob("hwmon/hwmon*/temp1_input"):
            millidegrees = read_int(sensor)
            if millidegrees is not None:
                return millidegrees / 1000
        return None


class JetsonProbe:
    """Jetson boards have no nvidia-smi and share one memory pool between CPU and GPU."""

    def __init__(self, root: Path = Path("/")) -> None:
        self._root = root

    def read(self) -> tuple[GpuInfo, ...]:
        if not (self._root / "etc/nv_tegra_release").exists():
            return ()
        model = (read_text(self._root / "proc/device-tree/model") or "NVIDIA Jetson").rstrip("\x00")
        return (
            GpuInfo(
                vendor="NVIDIA",
                name=model,
                vram_total_bytes=0,
                unified_memory=True,
                note="GPU memory is shared with system RAM.",
            ),
        )
