"""What can physically be wired to this machine: the raw material for home automation."""

from pathlib import Path

from brain.domain.hardware import Peripheral

_PATTERNS = (
    ("gpio", "dev/gpiochip*"),
    ("i2c", "dev/i2c-*"),
    ("spi", "dev/spidev*"),
    ("serial", "dev/ttyUSB*"),
    ("serial", "dev/ttyACM*"),
    ("camera", "dev/video*"),
    ("bluetooth", "sys/class/bluetooth/*"),
    ("audio", "dev/snd/pcmC*"),
)


class PeripheralProbe:
    def __init__(self, root: Path = Path("/")) -> None:
        self._root = root

    def read(self) -> tuple[Peripheral, ...]:
        return tuple(
            *self._find(kind, pattern) for kind, pattern in _PATTERNS
        )

    def _find(self, kind: str, pattern: str) -> list[Peripheral]:
        return [
            Peripheral(kind=kind, path=f"/{path.relative_to(self._root)}")
            for path in sorted(self._root.glob(pattern))
        ]
