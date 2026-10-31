"""Connects the application to the standard library's threading HTTP server.

Each connection gets its own thread. That keeps streaming code plainly sequential, is ample for a
home-scale hub, and scales across cores on free-threaded Python builds without any changes.
"""

import logging
import socket
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from brain import __version__
from brain.domain.errors import (
    AgentIncompatible,
    BrainError,
    CapabilityMissing,
    CloudAgentRefused,
    Conflict,
    Forbidden,
    InstallFailed,
    InvalidRequest,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    RuntimeRequestFailed,
    RuntimeUnavailable,
    Unauthorized,
)
from brain.interfaces.http.messages import EmptyResponse, JsonResponse, Request, Response
from brain.interfaces.http.router import Router
from brain.interfaces.http.security import SecurityPolicy

_log = logging.getLogger(__name__)
_IDLE_TIMEOUT_SECONDS = 75
_BASE_HEADERS = frozendict(
    {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
)
_CORS_MAX_AGE = "600"

# Listed from most to least specific, so subclasses are matched before their parents.
_STATUS_BY_ERROR: tuple[tuple[type[BrainError], int], ...] = (
    (InvalidRequest, 400),
    (Unauthorized, 401),
    (Forbidden, 403),
    (CloudAgentRefused, 403),
    (NotFound, 404),
    (MethodNotAllowed, 405),
    (AgentIncompatible, 409),
    (CapabilityMissing, 409),
    (Conflict, 409),
    (PayloadTooLarge, 413),
    (InstallFailed, 502),
    (RuntimeRequestFailed, 502),
    (RuntimeUnavailable, 503),
)


def status_of(error: BrainError) -> int:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return 500


def error_response(error: BrainError) -> JsonResponse:
    body: dict[str, str] = {"code": error.code, "message": error.message}
    if error.hint:
        body["hint"] = error.hint
    return JsonResponse({"error": body}, status=status_of(error))


class Application:
    """Applies security, finds the route, and turns failures into consistent JSON errors."""

    def __init__(self, *, router: Router, security: SecurityPolicy, max_body_bytes: int) -> None:
        self.router = router
        self.security = security
        self.max_body_bytes = max_body_bytes

    def handle(self, request: Request) -> Response:
        try:
            self.security.check_transport(request.headers)
            if request.method == "OPTIONS":
                return EmptyResponse()
            route = self.router.resolve(request.method, request.path)
            if not route.public:
                self.security.check_credentials(request.headers)
            return route.handler(
                Request(
                    method=request.method,
                    path=request.path,
                    query=request.query,
                    headers=request.headers,
                    params=route.params,
                    body=request.body,
                )
            )
        except BrainError as error:
            return error_response(error)
        except Exception:
            _log.exception("Unhandled error on %s %s", request.method, request.path)
            return JsonResponse(
                {"error": {"code": "internal_error", "message": "Something went wrong."}},
                status=500,
            )


class _Channel:
    """Implements ResponseChannel on top of the standard library's request handler."""

    def __init__(self, handler: _RequestHandler, cors_origin: str | None) -> None:
        self._handler = handler
        self._cors_origin = cors_origin

    def begin(self, status: int, headers: Mapping[str, str]) -> None:
        handler = self._handler
        handler.send_response(status)
        for name, value in (_BASE_HEADERS | headers).items():
            handler.send_header(name, value)
        if self._cors_origin:
            handler.send_header("Access-Control-Allow-Origin", self._cors_origin)
            handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            handler.send_header(
                "Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"
            )
            handler.send_header("Access-Control-Max-Age", _CORS_MAX_AGE)
            handler.send_header("Vary", "Origin")
        handler.end_headers()

    def write(self, data: bytes) -> None:
        self._handler.wfile.write(data)

    def write_chunk(self, data: bytes) -> None:
        if data:
            self._handler.wfile.write(b"%X\r\n%b\r\n" % (len(data), data))

    def end_chunks(self) -> None:
        self._handler.wfile.write(b"0\r\n\r\n")


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"LocalAIBrain/{__version__}"
    sys_version = ""
    timeout = _IDLE_TIMEOUT_SECONDS
    disable_nagle_algorithm = True  # Tokens should reach the reader as soon as they exist.
    server: BrainHttpServer

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        _log.debug("%s %s", self.address_string(), format % args)

    def _dispatch(self) -> None:
        application = self.server.application
        headers = frozendict({key.lower(): value for key, value in self.headers.items()})
        channel = _Channel(self, application.security.cors_origin(headers))
        try:
            response = self._respond(application, headers)
            response.send(channel)
        except BrokenPipeError, ConnectionResetError, TimeoutError:
            # The client went away mid-response, which is routine for a stream.
            self.close_connection = True
        except Exception:
            # Headers may already be out, so all that is left is to end the connection.
            _log.exception("Failed while answering %s %s", self.command, self.path)
            self.close_connection = True

    def _respond(self, application: Application, headers: frozendict[str, str]) -> Response:
        try:
            body = self._read_body(application.max_body_bytes, headers)
        except BrainError as error:
            self.close_connection = True
            return error_response(error)
        target = urlsplit(self.path)
        return application.handle(
            Request(
                method=self.command,
                path=target.path,
                query=frozendict(parse_qsl(target.query)),
                headers=headers,
                body=body,
            )
        )

    def _read_body(self, limit: int, headers: Mapping[str, str]) -> bytes:
        if "transfer-encoding" in headers:
            raise InvalidRequest("Chunked request bodies are not supported. Send Content-Length.")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError as error:
            raise InvalidRequest("Content-Length must be a number.") from error
        if length < 0:
            raise InvalidRequest("Content-Length must not be negative.")
        if length > limit:
            raise PayloadTooLarge(f"The request body is over the {limit} byte limit.")
        return self.rfile.read(length) if length else b""


class BrainHttpServer(ThreadingHTTPServer):
    daemon_threads = True  # A long-lived event stream must never block shutdown.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], application: Application) -> None:
        self.application = application
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _RequestHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        _log.debug("Connection error from %s", client_address, exc_info=True)


def build_router(controllers: Sequence[object]) -> Router:
    router = Router()
    for controller in controllers:
        controller.register(router)  # type: ignore[attr-defined]
    return router
