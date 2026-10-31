"""Who may talk to the brain.

This API can install software and control a house, so three separate attacks are covered:
  * DNS rebinding, where a website points its own domain at 127.0.0.1 (the Host check)
  * Cross-site requests from any page open in a browser on this network (the Origin check)
  * Other machines on the network, when the brain listens beyond loopback (the token)
"""

import hmac
from collections.abc import Collection, Mapping
from urllib.parse import urlsplit

from brain.domain.errors import Forbidden, Unauthorized

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _hostname_of(host_header: str) -> str:
    return (urlsplit(f"//{host_header}").hostname or "").lower()


def _is_loopback_name(hostname: str) -> bool:
    return hostname in _LOOPBACK_NAMES or hostname.endswith(".localhost")


class SecurityPolicy:
    def __init__(
        self,
        *,
        token: str | None,
        loopback_only: bool,
        allowed_hosts: Collection[str] = (),
        allowed_origins: Collection[str] = (),
    ) -> None:
        self._token = token
        self._loopback_only = loopback_only
        self._allowed_hosts = {host.lower() for host in allowed_hosts}
        self._allowed_origins = {origin.rstrip("/").lower() for origin in allowed_origins}

    @property
    def token_required(self) -> bool:
        return self._token is not None

    def check_transport(self, headers: Mapping[str, str]) -> None:
        """Applies to every request, including the public dashboard files."""
        host_header = headers.get("host", "")
        self._check_host(host_header)
        self._check_origin(headers.get("origin"), host_header)

    def check_credentials(self, headers: Mapping[str, str]) -> None:
        if self._token is None:
            return
        scheme, _, supplied = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip().encode("utf-8"), self._token.encode("utf-8")
        ):
            raise Unauthorized(
                "A valid API token is required.",
                hint="Send 'Authorization: Bearer <token>'. Run `brain token` to see it.",
            )

    def cors_origin(self, headers: Mapping[str, str]) -> str | None:
        origin = headers.get("origin")
        if origin and origin.rstrip("/").lower() in self._allowed_origins:
            return origin
        return None

    def _check_host(self, host_header: str) -> None:
        hostname = _hostname_of(host_header)
        if hostname in self._allowed_hosts:
            return
        if self._loopback_only and not _is_loopback_name(hostname):
            raise Forbidden(
                f"This brain does not answer to the host '{hostname or host_header}'.",
                hint="Add it to BRAIN_ALLOWED_HOSTS if it is really yours.",
            )

    def _check_origin(self, origin: str | None, host_header: str) -> None:
        if origin is None:
            return  # Non-browser programs send no Origin header, and need none.
        normalized = origin.rstrip("/").lower()
        if normalized in self._allowed_origins:
            return
        if urlsplit(normalized).netloc == host_header.lower():
            return
        raise Forbidden(
            f"Requests from the web page at {origin} are not allowed.",
            hint="Add the origin to BRAIN_ALLOWED_ORIGINS if you trust it.",
        )
