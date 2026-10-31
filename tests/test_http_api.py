"""The real HTTP server end to end, including its security rules and the Python SDK."""

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from brain.config import AuthMode, Settings, TokenStore
from brain.container import Brain
from brain.domain.errors import Forbidden, Unauthorized
from brain.interfaces.http.app import build_server
from brain.interfaces.http.security import SecurityPolicy
from brain.sdk import BrainApiError, BrainClient
from tests.fakes import FakeInspector, FakeOllama, NoRegistry

AGENT = "qwen3:4b-q4_K_M"
TOKEN = "correct-horse-battery-staple"


class ApiTestCase(unittest.TestCase):
    settings: dict[str, Any] = {}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ollama = FakeOllama().__enter__()
        self.addCleanup(self.ollama.__exit__)
        self.brain = Brain(
            Settings(data_dir=Path(directory.name), ollama_url=self.ollama.url, port=0, **self.settings)
        )
        self.brain.hardware = FakeInspector()
        self.brain.registry = NoRegistry()
        self.server = build_server(self.brain)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.brain.database.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, headers=None, raw_body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        encoded = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            connection.request(method, path, body=encoded, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            parsed = None
            if data and "json" in (response.getheader("Content-Type") or ""):
                parsed = json.loads(data)
            return response.status, parsed, response.headers, data
        finally:
            connection.close()

    def api(self, method, path, body=None, headers=None):
        return self.call(method, f"/api/v1{path}", body, headers)

    def mount_agent(self):
        self.ollama.add_model(AGENT, capabilities=("completion", "tools", "thinking"))
        status, job, *_ = self.api("POST", f"/agents/{AGENT}/mount?wait=1")
        self.assertEqual((status, job["state"]), (200, "succeeded"))


class RestTests(ApiTestCase):
    def test_health(self):
        status, body, *_ = self.api("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["token_required"])

    def test_hardware_is_json_with_enums_as_strings(self):
        _, body, *_ = self.api("GET", "/hardware")
        self.assertEqual(body["environment"]["deployment"], "bare_metal")
        self.assertIn("total_bytes", body["memory"])

    def test_runtime(self):
        _, body, *_ = self.api("GET", "/runtime")
        self.assertTrue(body["reachable"])
        self.assertTrue(body["installed"])

    def test_agents_report_availability_with_reasons(self):
        _, body, *_ = self.api("GET", "/agents")
        agents = {agent["name"]: agent for agent in body["agents"]}
        self.assertTrue(agents["smollm2:135m"]["compatibility"]["available"])
        too_big = agents["qwen3:235b"]["compatibility"]
        self.assertFalse(too_big["available"])
        self.assertEqual(too_big["findings"][0]["severity"], "blocker")

    def test_agent_names_with_slashes_and_colons_route_correctly(self):
        self.ollama.add_model("hf.co/user/model:Q4")
        for path in ("hf.co/user/model:Q4", "hf.co%2Fuser%2Fmodel%3AQ4"):
            status, body, *_ = self.api("GET", f"/agents/{path}")
            self.assertEqual((status, body["name"]), (200, "hf.co/user/model:Q4"))

    def test_lifecycle_over_http(self):
        status, job, *_ = self.api("POST", f"/agents/{AGENT}/install")
        self.assertEqual(status, 202)
        self.assertEqual(job["kind"], "install")
        self.brain.jobs.wait(job["id"], 5)
        self.assertEqual(self.api("GET", f"/agents/{AGENT}")[1]["state"], "installed")
        self.api("POST", f"/agents/{AGENT}/mount?wait=1")
        self.assertEqual(self.api("GET", f"/agents/{AGENT}")[1]["state"], "mounted")
        self.api("POST", f"/agents/{AGENT}/unmount?wait=1")
        self.assertEqual(self.api("GET", f"/agents/{AGENT}")[1]["state"], "installed")
        self.api("DELETE", f"/agents/{AGENT}?wait=1")
        self.assertEqual(self.api("GET", f"/agents/{AGENT}")[1]["state"], "available")

    def test_errors_have_a_consistent_shape_and_the_right_status(self):
        cases = [
            ("POST", f"/agents/{AGENT}/mount", None, 409, "agent_not_installed"),
            ("POST", "/agents/gpt-oss:120b-cloud/install", None, 403, "cloud_agent_refused"),
            ("POST", "/agents/qwen3:235b/install", {"force": "yes"}, 400, "invalid_request"),
            ("GET", "/jobs/nope", None, 404, "not_found"),
            ("GET", "/nothing/here", None, 404, "not_found"),
            ("PATCH", "/health", {}, 405, "method_not_allowed"),
        ]
        for method, path, body, status, code in cases:
            with self.subTest(path=path):
                actual, parsed, *_ = self.api(method, path, body)
                self.assertEqual((actual, parsed["error"]["code"]), (status, code))

    def test_a_typo_in_a_field_is_caught_rather_than_silently_ignored(self):
        self.mount_agent()
        status, body, *_ = self.api("POST", f"/agents/{AGENT}/chat", {"mesage": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("mesage", body["error"]["message"])

    def test_nested_payloads_are_validated(self):
        self.mount_agent()
        bad = {"messages": [{"role": "wizard", "content": "x"}]}
        status, body, *_ = self.api("POST", f"/agents/{AGENT}/chat", bad)
        self.assertEqual(status, 400)
        self.assertIn("role", body["error"]["message"])

    def test_invalid_json(self):
        status, body, *_ = self.call("POST", "/api/v1/agents/resolve", raw_body=b"{nope")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_oversized_body_is_refused_without_reading_it(self):
        limit = self.brain.settings.max_body_bytes
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(
                f"POST /api/v1/agents/resolve HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {limit + 1}\r\n\r\n".encode()
            )
            answer = raw.recv(4096)
        self.assertIn(b"413", answer.split(b"\r\n")[0])

    def test_chunked_upload_is_refused(self):
        status, body, *_ = self.call(
            "POST", "/api/v1/agents/resolve", raw_body=b"0\r\n\r\n", headers={"Transfer-Encoding": "chunked"}
        )
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_runtime_down_gives_503(self):
        self.brain.__dict__["ollama"].base_url = "http://127.0.0.1:9"
        self.brain.ollama._port = 9
        status, body, *_ = self.api("POST", f"/agents/{AGENT}/install")
        self.assertEqual((status, body["error"]["code"]), (503, "runtime_unavailable"))
        status, body, *_ = self.api("GET", "/agents")
        self.assertEqual(status, 200)
        self.assertFalse(body["runtime_online"])


class DeviceApiTests(ApiTestCase):
    def test_report_list_history_remove(self):
        status, device, *_ = self.api(
            "POST",
            "/devices/kitchen/readings",
            {"name": "Kitchen", "readings": [{"metric": "t", "value": 21.5, "unit": "C"}, {"metric": "door", "value": False}]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(device["name"], "Kitchen")
        devices = self.api("GET", "/devices")[1]["devices"]
        self.assertEqual(len(devices), 1)
        history = self.api("GET", "/devices/kitchen/readings?metric=t")[1]["readings"]
        self.assertEqual(history[0]["value"], 21.5)
        self.assertEqual(self.api("DELETE", "/devices/kitchen")[0], 204)
        self.assertEqual(self.api("DELETE", "/devices/kitchen")[0], 404)

    def test_reading_values_are_type_checked(self):
        status, *_ = self.api("POST", "/devices/x/readings", {"readings": [{"metric": "t", "value": [1, 2]}]})
        self.assertEqual(status, 400)


class StreamingTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.mount_agent()

    def _chat_connection(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", f"/api/v1/agents/{AGENT}/chat", body=json.dumps(body))
        return connection, connection.getresponse()

    def test_ndjson_stream(self):
        connection, response = self._chat_connection({"message": "hi", "stream": True})
        events = [json.loads(line) for line in response.read().splitlines()]
        connection.close()
        self.assertEqual(response.getheader("Content-Type"), "application/x-ndjson")
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "content"), "Hello there!")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["stats"]["completion_tokens"], 3)

    def test_validation_errors_are_real_statuses_even_when_streaming(self):
        self.api("POST", f"/agents/{AGENT}/unmount?wait=1")
        connection, response = self._chat_connection({"message": "hi", "stream": True})
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.read())["error"]["code"], "agent_not_mounted")
        connection.close()

    def test_failure_mid_stream_arrives_as_an_event(self):
        self.ollama.chat_error_after = 1
        connection, response = self._chat_connection({"message": "hi", "stream": True})
        events = [json.loads(line) for line in response.read().splitlines()]
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(events[-1]["type"], "error")

    def test_client_disconnect_stops_generation_and_keeps_the_partial_reply(self):
        self.ollama.reply_tokens = [f"t{index} " for index in range(200)]
        self.ollama.pause_after_first_token.set()
        connection, response = self._chat_connection({"message": "hi", "stream": True, "conversation_id": "gone"})
        self.assertEqual(json.loads(response.readline())["type"], "start")
        self.assertEqual(json.loads(response.readline())["text"], "t0 ")
        connection.sock.shutdown(socket.SHUT_RDWR)
        connection.close()
        self.ollama.release.set()

        deadline = time.monotonic() + 5
        messages = ()
        while time.monotonic() < deadline:
            messages = self.brain.chat.conversation("gone")[1]
            if len(messages) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(messages[-1].role.value, "assistant")
        self.assertTrue(messages[-1].interrupted)
        self.assertTrue(messages[-1].content.startswith("t0 "))
        self.assertTrue(
            self.ollama.client_went_away.wait(5), "closing the client must also stop the model generating"
        )

    def test_event_stream_delivers_readings_to_subscribers(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/api/v1/events")
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(response.readline().strip(), b": connected")
        self.assertEqual(response.readline().strip(), b"")
        self.api("POST", "/devices/hall/readings", {"readings": [{"metric": "motion", "value": True}]})
        lines = [response.readline().decode().strip() for _ in range(3)]
        connection.close()
        self.assertTrue(lines[0].startswith("id: "))
        self.assertEqual(lines[1], "event: device.reading")
        data = json.loads(lines[2].removeprefix("data: "))
        self.assertEqual(data["device"], "hall")
        self.assertIs(data["readings"][0]["value"], True)


class TransportSecurityTests(ApiTestCase):
    def test_dns_rebinding_is_blocked(self):
        status, body, *_ = self.api("GET", "/hardware", headers={"Host": "evil.example"})
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))

    def test_also_blocked_for_public_routes(self):
        self.assertEqual(self.call("GET", "/", headers={"Host": "evil.example"})[0], 403)

    def test_loopback_names_are_accepted(self):
        for host in ("127.0.0.1:8484", "localhost:8484", "[::1]:8484", "brain.localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.api("GET", "/health", headers={"Host": host})[0], 200)

    def test_cross_site_requests_are_blocked(self):
        for origin in ("https://evil.example", "null", f"http://127.0.0.1:{self.port}.evil.example"):
            with self.subTest(origin=origin):
                status, *_ = self.api(
                    "POST", f"/agents/{AGENT}/unmount", headers={"Origin": origin, "Host": f"127.0.0.1:{self.port}"}
                )
                self.assertEqual(status, 403)

    def test_same_origin_browser_requests_work(self):
        host = f"127.0.0.1:{self.port}"
        status, *_ = self.api("GET", "/runtime", headers={"Origin": f"http://{host}", "Host": host})
        self.assertEqual(status, 200)

    def test_programs_send_no_origin_and_are_fine(self):
        self.assertEqual(self.api("GET", "/runtime")[0], 200)

    def test_no_cors_headers_by_default(self):
        host = f"127.0.0.1:{self.port}"
        _, _, headers, _ = self.api("GET", "/runtime", headers={"Origin": f"http://{host}", "Host": host})
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_security_headers(self):
        _, _, headers, _ = self.call("GET", "/")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        policy = headers.get("Content-Security-Policy")
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("unsafe", policy)


class TokenSecurityTests(ApiTestCase):
    settings = {"auth": AuthMode.ALWAYS, "token": TOKEN}

    def test_api_requires_the_token(self):
        status, body, *_ = self.api("GET", "/hardware")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))

    def test_wrong_token_and_wrong_scheme(self):
        for header in ("Bearer wrong", f"Basic {TOKEN}", TOKEN, "Bearer "):
            with self.subTest(header=header):
                self.assertEqual(self.api("GET", "/hardware", headers={"Authorization": header})[0], 401)

    def test_right_token(self):
        self.assertEqual(self.api("GET", "/hardware", headers={"Authorization": f"Bearer {TOKEN}"})[0], 200)

    def test_token_in_query_string_is_not_accepted(self):
        self.assertEqual(self.api("GET", f"/hardware?token={TOKEN}")[0], 401)

    def test_health_and_dashboard_stay_public(self):
        status, body, *_ = self.api("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["token_required"])
        self.assertEqual(self.call("GET", "/")[0], 200)

    def test_every_other_api_route_is_protected(self):
        for method, path in (
            ("GET", "/agents"),
            ("POST", f"/agents/{AGENT}/install"),
            ("DELETE", f"/agents/{AGENT}"),
            ("POST", f"/agents/{AGENT}/chat"),
            ("GET", "/runtime"),
            ("POST", "/runtime/install"),
            ("GET", "/devices"),
            ("POST", "/devices/x/readings"),
            ("GET", "/events"),
            ("GET", "/jobs"),
            ("GET", "/conversations"),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.api(method, path)[0], 401)

    def test_sdk_uses_the_token(self):
        self.assertEqual(BrainClient(f"http://127.0.0.1:{self.port}", token=TOKEN).devices(), [])
        with self.assertRaises(BrainApiError) as caught:
            BrainClient(f"http://127.0.0.1:{self.port}", token="wrong").devices()
        self.assertEqual(caught.exception.status, 401)


class NetworkBindingTests(unittest.TestCase):
    def test_on_a_network_any_host_name_works_but_the_token_is_what_protects_it(self):
        policy = SecurityPolicy(token=TOKEN, loopback_only=False)
        policy.check_transport({"host": "192.168.1.50:8484"})
        policy.check_transport({"host": "brain.home:8484", "origin": "http://brain.home:8484"})
        with self.assertRaises(Forbidden):
            policy.check_transport({"host": "192.168.1.50:8484", "origin": "http://evil.example"})
        with self.assertRaises(Unauthorized):
            policy.check_credentials({})

    def test_allowed_hosts_extend_a_loopback_brain(self):
        policy = SecurityPolicy(token=None, loopback_only=True, allowed_hosts=("Brain.Home",))
        policy.check_transport({"host": "brain.home:8484"})
        with self.assertRaises(Forbidden):
            policy.check_transport({"host": "other.home:8484"})

    def test_listening_beyond_loopback_forces_a_token(self):
        self.assertTrue(Settings(host="0.0.0.0").requires_token)
        self.assertFalse(Settings(host="127.0.0.1").requires_token)
        self.assertFalse(Settings(host="0.0.0.0", auth=AuthMode.OFF).requires_token)
        self.assertTrue(Settings(host="127.0.0.1", auth=AuthMode.ALWAYS).requires_token)

    def test_generated_token_is_private_and_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "new")
            token = store.load_or_create()
            self.assertGreater(len(token), 30)
            self.assertEqual(store.load_or_create(), token)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class CorsTests(ApiTestCase):
    settings = {"allowed_origins": ("http://games.local:3000",)}

    def test_allowed_origin_gets_cors_headers(self):
        status, _, headers, _ = self.api(
            "GET", "/health", headers={"Origin": "http://games.local:3000"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://games.local:3000")

    def test_preflight(self):
        status, _, headers, _ = self.api(
            "OPTIONS", f"/agents/{AGENT}/chat", headers={"Origin": "http://games.local:3000"}
        )
        self.assertEqual(status, 204)
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods"))

    def test_other_origins_still_blocked(self):
        status, _, headers, _ = self.api("GET", "/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))


class DashboardTests(ApiTestCase):
    def test_serves_index_and_assets_with_types(self):
        cases = {"/": "text/html", "/app.css": "text/css", "/js/app.js": "text/javascript", "/favicon.svg": "image/svg+xml"}
        for path, expected in cases.items():
            with self.subTest(path=path):
                status, _, headers, data = self.call("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers.get("Content-Type").startswith(expected))
                self.assertGreater(len(data), 0)

    def test_etag_allows_cheap_revalidation(self):
        _, _, headers, _ = self.call("GET", "/app.css")
        status, _, _, data = self.call("GET", "/app.css", headers={"If-None-Match": headers.get("ETag")})
        self.assertEqual((status, data), (304, b""))

    def test_path_traversal_finds_nothing(self):
        for path in ("/../../../etc/passwd", "/js/../../config.py", "/%2e%2e/%2e%2e/etc/passwd", "/__init__.py"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 404)

    def test_page_loads_nothing_from_outside(self):
        """Local-only means local-only: no CDN, no web font, no analytics."""
        root = Path(__file__).parent.parent / "brain/web"
        for path in [*root.rglob("*.html"), *root.rglob("*.css"), *root.rglob("*.js")]:
            text = path.read_text(encoding="utf-8")
            for forbidden in ("cdn.", "googleapis", "gstatic", "unpkg", "jsdelivr", "cloudflare"):
                self.assertNotIn(forbidden, text, f"{path.name} references {forbidden}")


class SdkTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client = BrainClient(f"http://127.0.0.1:{self.port}")

    def test_full_journey(self):
        self.assertTrue(self.client.runtime()["reachable"])
        self.assertEqual(self.client.install(AGENT)["state"], "succeeded")
        self.assertEqual(self.client.mount(AGENT)["state"], "succeeded")
        self.assertEqual(self.client.ask(AGENT, "hi"), "Hello there!")
        kinds = [event["type"] for event in self.client.stream(AGENT, message="hi")]
        self.assertEqual((kinds[0], kinds[-1]), ("start", "done"))
        self.client.unmount(AGENT)
        self.client.uninstall(AGENT)
        self.assertEqual(self.client.agent(AGENT)["state"], "available")

    def test_report_handles_units_and_bare_values(self):
        device = self.client.report("kitchen", name="Kitchen", temperature=(21.5, "°C"), open=False)
        readings = {reading["metric"]: reading for reading in device["latest"]}
        self.assertEqual(readings["temperature"]["unit"], "°C")
        self.assertIs(readings["open"]["value"], False)

    def test_failed_job_raises(self):
        self.ollama.pull_error = "no such model"
        with self.assertRaises(BrainApiError) as caught:
            self.client.install(AGENT)
        self.assertIn("no such model", str(caught.exception))

    def test_errors_carry_code_and_hint(self):
        self.ollama.add_model(AGENT)
        with self.assertRaises(BrainApiError) as caught:
            self.client.ask(AGENT, "hi")
        self.assertEqual(caught.exception.code, "agent_not_mounted")
        self.assertIn("auto_mount", caught.exception.hint)

    def test_events_follow(self):
        received = []

        def follow():
            for event_type, data in self.client.events():
                received.append((event_type, data))
                return

        thread = threading.Thread(target=follow, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not received and time.monotonic() < deadline:
            self.client.report("hall", motion=True)
            time.sleep(0.05)
        thread.join(3)
        self.assertEqual(received[0][0], "device.reading")
