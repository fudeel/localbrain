"""Test doubles. FakeOllama speaks real HTTP, using the response shapes seen from Ollama 0.40."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import UTC, datetime
from pathlib import Path

from brain.domain.hardware import (
    CpuInfo,
    Deployment,
    DiskInfo,
    EnvironmentInfo,
    GpuInfo,
    HardwareProfile,
    MemoryInfo,
)

GIB = 1024**3


def make_hardware(
    *,
    ram_gib: float = 8,
    free_ram_gib: float | None = None,
    disk_free_gib: float = 100,
    cores: float = 8,
    architecture: str = "x86_64",
    features: tuple[str, ...] = ("avx2",),
    gpus: tuple[GpuInfo, ...] = (),
    limited_by_cgroup: bool = False,
) -> HardwareProfile:
    total = int(ram_gib * GIB)
    return HardwareProfile(
        cpu=CpuInfo(
            model="Test CPU",
            architecture=architecture,
            logical_cores=int(cores),
            effective_cores=cores,
            features=features,
        ),
        memory=MemoryInfo(
            total_bytes=total,
            available_bytes=int((free_ram_gib if free_ram_gib is not None else ram_gib) * GIB),
            host_total_bytes=total,
            limited_by_cgroup=limited_by_cgroup,
        ),
        disk=DiskInfo(path="/data", total_bytes=int(500 * GIB), free_bytes=int(disk_free_gib * GIB)),
        environment=EnvironmentInfo(
            hostname="test", operating_system="Test Linux", kernel="6.8", deployment=Deployment.BARE_METAL
        ),
        gpus=gpus,
        taken_at=datetime.now(UTC),
    )


def make_gpu(vram_gib: float, *, supported: bool = True, unified: bool = False) -> GpuInfo:
    return GpuInfo(
        vendor="NVIDIA",
        name="Test GPU",
        vram_total_bytes=int(vram_gib * GIB),
        supported=supported,
        unified_memory=unified,
    )


class FakeInspector:
    def __init__(self, profile: HardwareProfile | None = None) -> None:
        self.profile = profile or make_hardware()

    def snapshot(self) -> HardwareProfile:
        return self.profile


class NoRegistry:
    """Stands in for the online registry, so tests never touch the network."""

    def __init__(self, entries: dict | None = None) -> None:
        self.entries = entries or {}

    def lookup(self, name):
        return self.entries.get(name)


class SilentSink:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.cancelled = False

    def report(self, message=None, *, completed=None, total=None) -> None:
        if message:
            self.messages.append(message)

    def raise_if_cancelled(self) -> None:
        from brain.domain.errors import JobCancelled

        if self.cancelled:
            raise JobCancelled("Cancelled.")


class FakeOllama:
    """A small Ollama over real HTTP, with hooks for slow and failing behaviour."""

    def __init__(self) -> None:
        self.installed: dict[str, dict] = {}
        self.loaded: dict[str, int] = {}
        self.chat_requests: list[dict] = []
        self.reply_tokens: list[str] = ["Hello", " there", "!"]
        self.thinking_tokens: list[str] = []
        self.pull_error: str | None = None
        self.load_error: str | None = None
        self.chat_error_after: int | None = None
        self.pause_after_first_token = threading.Event()  # When set, waits on `release`.
        self.release = threading.Event()
        self.client_went_away = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True)

    def __enter__(self) -> "FakeOllama":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def add_model(
        self,
        name: str,
        *,
        size: int = 2 * GIB,
        capabilities: tuple[str, ...] = ("completion", "tools"),
        remote: bool = False,
    ) -> None:
        self.installed[name] = {"size": size, "capabilities": capabilities, "remote": remote}

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                pass

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else {}

            def _json(self, status: int, data: dict) -> None:
                encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _begin_stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

            def _line(self, data: dict) -> None:
                line = json.dumps(data).encode() + b"\n"
                self.wfile.write(b"%X\r\n%b\r\n" % (len(line), line))
                self.wfile.flush()

            def _end_stream(self) -> None:
                self.wfile.write(b"0\r\n\r\n")

            def do_GET(self) -> None:
                match self.path:
                    case "/api/version":
                        self._json(200, {"version": "0.40.13"})
                    case "/api/tags":
                        self._json(200, {"models": [self._tag(n, m) for n, m in fake.installed.items()]})
                    case "/api/ps":
                        self._json(200, {"models": [self._ps(n, k) for n, k in fake.loaded.items()]})
                    case _:
                        self._json(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                name = self._body()["model"]
                if fake.installed.pop(name, None) is None:
                    self._json(404, {"error": f"model '{name}' not found"})
                else:
                    self._json(200, {})

            def do_POST(self) -> None:
                body = self._body()
                match self.path:
                    case "/api/pull":
                        self._pull(body)
                    case "/api/generate" | "/api/embed":
                        self._keep_alive(body)
                    case "/api/chat":
                        self._chat(body)
                    case _:
                        self._json(404, {"error": "not found"})

            def _tag(self, name: str, model: dict) -> dict:
                entry = {
                    "name": name,
                    "model": name,
                    "size": model["size"],
                    "digest": "abc",
                    "modified_at": "2026-10-31T16:27:46.270339675+01:00",
                    "details": {"family": "fake", "parameter_size": "4B", "quantization_level": "Q4_K_M"},
                    "capabilities": list(model["capabilities"]),
                }
                if model["remote"]:
                    entry |= {"remote_model": name, "remote_host": "https://ollama.com"}
                return entry

            def _ps(self, name: str, keep_alive: int) -> dict:
                model = fake.installed[name]
                expires = "2319-02-10T16:37:42.597349444+01:00" if keep_alive < 0 else "2026-10-31T16:37:42Z"
                return {
                    "name": name,
                    "model": name,
                    "size": int(model["size"] * 1.26),
                    "size_vram": 0,
                    "expires_at": expires,
                    "context_length": 4096,
                }

            def _pull(self, body: dict) -> None:
                self._begin_stream()
                self._line({"status": "pulling manifest"})
                if fake.pull_error:
                    self._line({"error": fake.pull_error})
                    self._end_stream()
                    return
                total = 1000
                self._line({"status": "pulling abc123", "digest": "abc", "total": total, "completed": 500})
                self._line({"status": "pulling abc123", "digest": "abc", "total": total, "completed": total})
                self._line({"status": "verifying sha256 digest"})
                self._line({"status": "writing manifest"})
                self._line({"status": "success"})
                fake.add_model(body["model"])
                self._end_stream()

            def _keep_alive(self, body: dict) -> None:
                name = body["model"]
                if name not in fake.installed:
                    self._json(404, {"error": f"model '{name}' not found"})
                    return
                if self.path == "/api/generate" and "completion" not in fake.installed[name]["capabilities"]:
                    self._json(400, {"error": f"{name} does not support generate"})
                    return
                if fake.load_error and body.get("keep_alive") != 0:
                    self._json(500, {"error": fake.load_error})
                    return
                if body.get("keep_alive") == 0:
                    fake.loaded.pop(name, None)
                else:
                    fake.loaded[name] = body.get("keep_alive", 300)
                self._json(200, {"embeddings": [[0.1, 0.2]] if self.path == "/api/embed" else [], "done": True})

            def _chat(self, body: dict) -> None:
                fake.chat_requests.append(body)
                if body["model"] not in fake.installed:
                    self._json(404, {"error": f"model '{body['model']}' not found"})
                    return
                if body.get("think") and "thinking" not in fake.installed[body["model"]]["capabilities"]:
                    self._json(400, {"error": f"{body['model']} does not support thinking"})
                    return
                self._begin_stream()
                try:
                    for token in fake.thinking_tokens:
                        self._line({"message": {"role": "assistant", "content": "", "thinking": token}, "done": False})
                    for index, token in enumerate(fake.reply_tokens):
                        if fake.chat_error_after == index:
                            self._line({"error": "model runner crashed"})
                            self._end_stream()
                            return
                        self._line({"message": {"role": "assistant", "content": token}, "done": False})
                        if fake.pause_after_first_token.is_set():
                            fake.release.wait(10)
                            time.sleep(0.005)
                    self._line(
                        {
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 10,
                            "eval_count": 3,
                            "eval_duration": 1_000_000_000,
                            "total_duration": 2_000_000_000,
                        }
                    )
                    self._end_stream()
                except OSError:
                    fake.client_went_away.set()

        return Handler


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Builds a fake filesystem, such as a pretend /proc and /sys, for a hardware probe."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
