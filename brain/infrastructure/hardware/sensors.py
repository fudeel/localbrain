"""Readings from the machine itself. Boards vary wildly, so an empty result is normal."""

import re
from pathlib import Path

from brain.domain.hardware import SensorValue
from brain.infrastructure.hardware.fs import read_int, read_text

_HWMON_INPUT = re.compile(r"(temp|fan|in|power)(\d+)_input")


class ThermalZoneProbe:
    def __init__(self, root: Path = Path("/")) -> None:
        self._base = root / "sys/class/thermal"

    def read(self) -> tuple[SensorValue, ...]:
        return tuple(
            sensor
            for zone in sorted(self._base.glob("thermal_zone*"))
            if (sensor := self._read_zone(zone))
        )

    @staticmethod
    def _read_zone(zone: Path) -> SensorValue | None:
        millidegrees = read_int(zone / "temp")
        if millidegrees is None:
            return None
        return SensorValue(
            source=zone.name,
            label=read_text(zone / "type") or zone.name,
            kind="temperature",
            value=millidegrees / 1000,
            unit="°C",
        )


class HwmonProbe:
    _SCALES = {
        "temp": ("temperature", 1000, "°C"),
        "fan": ("fan", 1, "RPM"),
        "in": ("voltage", 1000, "V"),
        "power": ("power", 1_000_000, "W"),
    }

    def __init__(self, root: Path = Path("/")) -> None:
        self._base = root / "sys/class/hwmon"

    def read(self) -> tuple[SensorValue, ...]:
        return tuple(*self._read_chip(chip) for chip in sorted(self._base.glob("hwmon*")))

    def _read_chip(self, chip: Path) -> list[SensorValue]:
        chip_name = read_text(chip / "name") or chip.name
        sensors = []
        for path in sorted(chip.glob("*_input")):
            match = _HWMON_INPUT.fullmatch(path.name)
            raw = read_int(path)
            if match is None or raw is None:
                continue
            prefix, index = match.groups()
            kind, scale, unit = self._SCALES[prefix]
            label = read_text(chip / f"{prefix}{index}_label") or f"{kind} {index}"
            sensors.append(
                SensorValue(
                    source=chip_name,
                    label=f"{chip_name} {label}",
                    kind=kind,
                    value=raw / scale,
                    unit=unit,
                )
            )
        return sensors


class BatteryProbe:
    def __init__(self, root: Path = Path("/")) -> None:
        self._base = root / "sys/class/power_supply"

    def read(self) -> tuple[SensorValue, ...]:
        return tuple(
            SensorValue(
                source=supply.name,
                label=f"{supply.name} charge",
                kind="battery",
                value=float(capacity),
                unit="%",
            )
            for supply in sorted(self._base.glob("*"))
            if (capacity := read_int(supply / "capacity")) is not None
        )
