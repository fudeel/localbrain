"""Reads real download sizes from registry manifests, without downloading the model itself."""

lazy import urllib.error
lazy import urllib.request

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from brain.domain.agents import AgentName, AgentSource, AgentSpec, Capability
from brain.domain.errors import NotFound

_log = logging.getLogger(__name__)
_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
_DEFAULT_REGISTRY = "registry.ollama.ai"
# A fixed list: agent names come from API callers, and must not steer requests to arbitrary hosts.
_TRUSTED_REGISTRIES = frozenset({_DEFAULT_REGISTRY, "hf.co", "huggingface.co"})
_LOOKUP_TIMEOUT_SECONDS = 8

type ManifestFetcher = Callable[[str], dict[str, Any]]


def _fetch_manifest(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": _ACCEPT})
    with urllib.request.urlopen(request, timeout=_LOOKUP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def manifest_url(name: AgentName) -> str | None:
    """Maps `model`, `user/model` and `host/user/model` the same way Ollama resolves them."""
    parts = name.model.split("/")
    if "." in parts[0] and len(parts) > 1:
        host, *path = parts
    else:
        host, path = _DEFAULT_REGISTRY, parts
    if host not in _TRUSTED_REGISTRIES:
        return None
    if host == _DEFAULT_REGISTRY and len(path) == 1:
        path = ["library", *path]
    return f"https://{host}/v2/{'/'.join(path)}/manifests/{name.tag}"


class OllamaRegistry:
    def __init__(self, fetch: ManifestFetcher = _fetch_manifest) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._cache: dict[str, AgentSpec] = {}

    def lookup(self, name: AgentName) -> AgentSpec | None:
        """Returns None when the answer is unknowable, for example offline.

        Raises NotFound when the registry says this agent does not exist.
        """
        with self._lock:
            if cached := self._cache.get(name):
                return cached
        url = manifest_url(name)
        if url is None:
            return None
        try:
            manifest = self._fetch(url)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                raise NotFound(f"The registry has no agent called {name}.") from error
            _log.info("Registry lookup for %s failed: %s", name, error)
            return None
        except (OSError, ValueError) as error:
            _log.info("Registry lookup for %s failed: %s", name, error)
            return None
        spec = self._to_spec(name, manifest)
        with self._lock:
            self._cache[name] = spec
        return spec

    @staticmethod
    def _to_spec(name: AgentName, manifest: dict[str, Any]) -> AgentSpec:
        layers = manifest.get("layers") or ()
        layer_types = {layer.get("mediaType", "").rsplit(".", 1)[-1] for layer in layers}
        size = sum(layer.get("size", 0) for layer in layers)
        size += (manifest.get("config") or {}).get("size", 0)
        capabilities = {Capability.VISION} if "projector" in layer_types else set()
        return AgentSpec(
            name=name,
            family=name.model.rsplit("/", 1)[-1],
            size_bytes=size,
            capabilities=frozenset(capabilities),
            source=AgentSource.REGISTRY,
        )
