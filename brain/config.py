"""Settings, layered: built-in defaults, then brain.toml, then BRAIN_* variables, then CLI flags."""

lazy import secrets
lazy import tomllib

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from brain.domain.errors import InvalidRequest
from brain.validation import validate

DEFAULT_PORT = 8484
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class AuthMode(StrEnum):
    AUTO = "auto"  # A token is required as soon as the brain listens beyond this machine.
    ALWAYS = "always"
    OFF = "off"


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(base) / "local-ai-brain"


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ollama_url: str = DEFAULT_OLLAMA_URL
    data_dir: Path = field(default_factory=_default_data_dir)
    auth: AuthMode = AuthMode.AUTO
    token: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    auto_start_ollama: bool = True
    auto_install_ollama: bool = False
    restore_mounts: bool = True
    reading_retention_days: int = 30
    max_body_bytes: int = 32 * 1024 * 1024

    @property
    def is_loopback_only(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    @property
    def requires_token(self) -> bool:
        match self.auth:
            case AuthMode.ALWAYS:
                return True
            case AuthMode.OFF:
                return False
            case AuthMode.AUTO:
                return not self.is_loopback_only


class _SettingsFile(TypedDict, closed=True):
    host: NotRequired[str]
    port: NotRequired[int]
    ollama_url: NotRequired[str]
    data_dir: NotRequired[str]
    auth: NotRequired[str]
    token: NotRequired[str]
    allowed_hosts: NotRequired[list[str]]
    allowed_origins: NotRequired[list[str]]
    auto_start_ollama: NotRequired[bool]
    auto_install_ollama: NotRequired[bool]
    restore_mounts: NotRequired[bool]
    reading_retention_days: NotRequired[int]
    max_body_bytes: NotRequired[int]


def _parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    raise ValueError(f"'{text}' is not true or false")


def _parse_list(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


_COERCE = frozendict(
    port=int,
    data_dir=Path,
    auth=AuthMode,
    allowed_hosts=_parse_list,
    allowed_origins=_parse_list,
    auto_start_ollama=_parse_bool,
    auto_install_ollama=_parse_bool,
    restore_mounts=_parse_bool,
    reading_retention_days=int,
    max_body_bytes=int,
)


def normalize_ollama_host(raw: str) -> str:
    """OLLAMA_HOST often holds a bare bind address such as `0.0.0.0:11434`, not a usable URL."""
    value = raw.strip()
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    scheme, _, rest = value.partition("://")
    host, _, port = rest.partition(":")
    host = host.rstrip("/")
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port.rstrip('/') or 11434}"


class SettingsLoader:
    ENV_PREFIX = "BRAIN_"

    def __init__(
        self, *, environ: Mapping[str, str] = os.environ, config_path: Path | None = None
    ) -> None:
        self._environ = environ
        self._config_path = config_path

    def load(self, overrides: Mapping[str, Any] | None = None) -> Settings:
        settings = Settings()
        for layer in (self._from_file(), self._from_environment(), overrides or {}):
            settings = replace(settings, **{k: v for k, v in layer.items() if v is not None})
        return settings

    def _path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        if configured := self._environ.get(f"{self.ENV_PREFIX}CONFIG"):
            return Path(configured)
        base = self._environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        return Path(base) / "local-ai-brain/brain.toml"

    def _from_file(self) -> dict[str, Any]:
        path = self._path()
        if not path.is_file():
            return {}
        with path.open("rb") as file:
            try:
                document = tomllib.load(file)
            except tomllib.TOMLDecodeError as error:
                raise InvalidRequest(f"{path} is not valid TOML: {error}") from error
        validate(_SettingsFile, document, path=str(path))
        result: dict[str, Any] = dict(document)
        if "data_dir" in result:
            result["data_dir"] = Path(result["data_dir"])
        if "auth" in result:
            result["auth"] = AuthMode(result["auth"])
        for key in ("allowed_hosts", "allowed_origins"):
            if key in result:
                result[key] = tuple(result[key])
        return result

    def _from_environment(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in fields(Settings):
            raw = self._environ.get(f"{self.ENV_PREFIX}{item.name.upper()}")
            if raw is None:
                continue
            convert = _COERCE.get(item.name, str)
            try:
                result[item.name] = convert(raw)
            except ValueError as error:
                raise InvalidRequest(
                    f"{self.ENV_PREFIX}{item.name.upper()} is invalid: {error}"
                ) from error
        if "ollama_url" not in result and (host := self._environ.get("OLLAMA_HOST")):
            result["ollama_url"] = normalize_ollama_host(host)
        return result


class TokenStore:
    """Keeps the API token in a file only this user can read."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "token"

    @property
    def path(self) -> Path:
        return self._path

    def load_or_create(self) -> str:
        try:
            existing = self._path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700, parent_mode=0o755)
        token = secrets.token_urlsafe(32)
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(token)
        return token
