"""Maps method and path to a handler. `{name}` matches one segment, `{name+}` matches several."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote

from brain.domain.errors import MethodNotAllowed, NotFound
from brain.interfaces.http.messages import Request, Response

type Handler = Callable[[Request], Response]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)(\+?)\}")


def _compile(template: str) -> re.Pattern[str]:
    pattern = ""
    position = 0
    for match in _PLACEHOLDER.finditer(template):
        pattern += re.escape(template[position : match.start()])
        name, greedy = match.groups()
        # Agent names can contain "/", as in hf.co/user/model:tag.
        pattern += f"(?P<{name}>.+?)" if greedy else f"(?P<{name}>[^/]+)"
        position = match.end()
    pattern += re.escape(template[position:])
    return re.compile(pattern)


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    public: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    handler: Handler
    params: frozendict[str, str]
    public: bool


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, template: str, handler: Handler, *, public: bool = False) -> None:
        self._routes.append(Route(method, _compile(template), handler, public))

    def resolve(self, method: str, path: str) -> ResolvedRoute:
        allowed = False
        for route in self._routes:
            match = route.pattern.fullmatch(path)
            if match is None:
                continue
            allowed = True
            if route.method == method:
                return ResolvedRoute(
                    handler=route.handler,
                    params=frozendict(
                        {key: unquote(value) for key, value in match.groupdict().items()}
                    ),
                    public=route.public,
                )
        if allowed:
            raise MethodNotAllowed(f"{method} is not supported on {path}.")
        raise NotFound(f"Nothing lives at {path}.")
