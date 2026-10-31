"""Tolerant readers for /proc and /sys, where any file may be absent or unreadable."""

from pathlib import Path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_key_values(text: str | None, separator: str = ":") -> dict[str, str]:
    """Parses `key: value` lines, keeping the first occurrence of each key."""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, found, value = line.partition(separator)
        if found:
            values.setdefault(key.strip(), value.strip())
    return values
