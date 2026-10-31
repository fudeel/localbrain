"""Configuration, validation, registry, catalog and the command line."""

import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict
from unittest import mock

from brain.config import AuthMode, Settings, SettingsLoader, normalize_ollama_host
from brain.container import Brain
from brain.domain.agents import AgentName, Capability
from brain.domain.errors import InvalidRequest, NotFound
from brain.infrastructure.catalog import JsonCatalog
from brain.infrastructure.ollama.registry import OllamaRegistry, manifest_url
from brain.interfaces import cli
from brain.interfaces.http.router import Router
from brain.domain.errors import MethodNotAllowed
from brain.validation import validate
from tests.fakes import FakeInspector, FakeOllama, NoRegistry, make_hardware


class _Inner(TypedDict, closed=True):
    x: int


class _Schema(TypedDict, closed=True):
    name: str
    size: NotRequired[int]
    ratio: NotRequired[float]
    flag: NotRequired[bool]
    maybe: NotRequired[str | None]
    mode: NotRequired[Literal["a", "b"]]
    items: NotRequired[list[str]]
    inner: NotRequired[_Inner]
    anything: NotRequired[dict[str, Any]]


class ValidationTests(unittest.TestCase):
    def test_accepts_valid_data(self):
        data = {"name": "n", "size": 1, "ratio": 2, "flag": True, "maybe": None, "mode": "a", "items": ["x"], "inner": {"x": 1}, "anything": {"k": [1]}}
        self.assertEqual(validate(_Schema, data), data)

    def test_rejections(self):
        cases = [
            ("not an object", ["a"]),
            ("missing", {}),
            ("unknown", {"name": "n", "nmae": "n"}),
            ("wrong type", {"name": 1}),
            ("bool is not int", {"name": "n", "size": True}),
            ("bool is not float", {"name": "n", "ratio": False}),
            ("int is not bool", {"name": "n", "flag": 1}),
            ("bad literal", {"name": "n", "mode": "c"}),
            ("list item", {"name": "n", "items": [1]}),
            ("not a list", {"name": "n", "items": "x"}),
            ("nested unknown", {"name": "n", "inner": {"x": 1, "y": 2}}),
            ("nested type", {"name": "n", "inner": {"x": "1"}}),
            ("union", {"name": "n", "maybe": 1}),
        ]
        for label, data in cases:
            with self.subTest(label), self.assertRaises(InvalidRequest):
                validate(_Schema, data)

    def test_error_names_the_path(self):
        with self.assertRaises(InvalidRequest) as caught:
            validate(_Schema, {"name": "n", "inner": {"x": "1"}})
        self.assertIn("body.inner.x", caught.exception.message)


class SettingsTests(unittest.TestCase):
    def load(self, *, environ=None, file_text=None, overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brain.toml"
            if file_text is not None:
                path.write_text(file_text, encoding="utf-8")
            return SettingsLoader(environ=environ or {}, config_path=path).load(overrides)

    def test_defaults_are_safe(self):
        settings = self.load()
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertFalse(settings.auto_install_ollama)
        self.assertFalse(settings.requires_token)

    def test_layers_override_in_order(self):
        settings = self.load(
            file_text='port = 1\nhost = "0.0.0.0"\nreading_retention_days = 3\n',
            environ={"BRAIN_PORT": "2", "BRAIN_HOST": "10.0.0.1"},
            overrides={"port": 3, "host": None},
        )
        self.assertEqual((settings.port, settings.host, settings.reading_retention_days), (3, "10.0.0.1", 3))

    def test_toml_is_converted(self):
        settings = self.load(file_text='auth = "always"\ndata_dir = "/x"\nallowed_origins = ["http://a"]\n')
        self.assertIs(settings.auth, AuthMode.ALWAYS)
        self.assertEqual(settings.data_dir, Path("/x"))
        self.assertEqual(settings.allowed_origins, ("http://a",))

    def test_typo_in_the_file_is_an_error(self):
        with self.assertRaises(InvalidRequest):
            self.load(file_text="prot = 1\n")

    def test_broken_toml_is_an_error(self):
        with self.assertRaises(InvalidRequest):
            self.load(file_text="port = = 1\n")

    def test_environment_values_are_parsed(self):
        settings = self.load(
            environ={"BRAIN_AUTO_INSTALL_OLLAMA": "yes", "BRAIN_RESTORE_MOUNTS": "off", "BRAIN_ALLOWED_HOSTS": "a, b,", "BRAIN_AUTH": "off"}
        )
        self.assertTrue(settings.auto_install_ollama)
        self.assertFalse(settings.restore_mounts)
        self.assertEqual(settings.allowed_hosts, ("a", "b"))
        self.assertIs(settings.auth, AuthMode.OFF)

    def test_bad_environment_value_is_a_clear_error(self):
        for environ in ({"BRAIN_PORT": "abc"}, {"BRAIN_RESTORE_MOUNTS": "maybe"}, {"BRAIN_AUTH": "nope"}):
            with self.subTest(environ), self.assertRaises(InvalidRequest):
                self.load(environ=environ)

    def test_ollama_host_is_honoured_and_normalized(self):
        self.assertEqual(self.load(environ={"OLLAMA_HOST": "0.0.0.0:11500"}).ollama_url, "http://127.0.0.1:11500")
        explicit = self.load(environ={"OLLAMA_HOST": "0.0.0.0:1", "BRAIN_OLLAMA_URL": "http://gpu-box:11434"})
        self.assertEqual(explicit.ollama_url, "http://gpu-box:11434")

    def test_normalize(self):
        self.assertEqual(normalize_ollama_host("gpu-box"), "http://gpu-box:11434")
        self.assertEqual(normalize_ollama_host("https://gpu-box:443/"), "https://gpu-box:443")
        self.assertEqual(normalize_ollama_host(":11434"), "http://127.0.0.1:11434")


class RegistryTests(unittest.TestCase):
    MANIFEST = {
        "config": {"size": 100},
        "layers": [
            {"mediaType": "application/vnd.ollama.image.model", "size": 1000},
            {"mediaType": "application/vnd.ollama.image.projector", "size": 500},
        ],
    }

    def test_manifest_urls(self):
        cases = {
            "qwen3:4b": "https://registry.ollama.ai/v2/library/qwen3/manifests/4b",
            "user/model:v1": "https://registry.ollama.ai/v2/user/model/manifests/v1",
            "hf.co/user/model:Q4": "https://hf.co/v2/user/model/manifests/Q4",
        }
        for name, expected in cases.items():
            self.assertEqual(manifest_url(AgentName.parse(name)), expected)

    def test_untrusted_hosts_are_never_contacted(self):
        for name in ("169.254.169.254/latest/meta-data:x", "evil.example/a:b", "192.168.1.1/admin:x"):
            calls = []
            registry = OllamaRegistry(fetch=calls.append)
            self.assertIsNone(registry.lookup(AgentName.parse(name)))
            self.assertEqual(calls, [])

    def test_size_and_vision_come_from_the_manifest(self):
        spec = OllamaRegistry(fetch=lambda _url: self.MANIFEST).lookup(AgentName.parse("m:v1"))
        self.assertEqual(spec.size_bytes, 1600)
        self.assertIn(Capability.VISION, spec.capabilities)

    def test_results_are_cached(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return self.MANIFEST

        registry = OllamaRegistry(fetch=fetch)
        registry.lookup(AgentName.parse("m:v1"))
        registry.lookup(AgentName.parse("m:v1"))
        self.assertEqual(len(calls), 1)

    def test_missing_model_says_so(self):
        def fetch(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO())

        with self.assertRaises(NotFound):
            OllamaRegistry(fetch=fetch).lookup(AgentName.parse("m:v1"))

    def test_offline_is_unknown_not_an_error(self):
        def fetch(_url):
            raise OSError("no route to host")

        self.assertIsNone(OllamaRegistry(fetch=fetch).lookup(AgentName.parse("m:v1")))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = JsonCatalog()

    def test_includes_the_users_model_with_its_real_size(self):
        spec = self.catalog.find(AgentName("qwen3:4b-q4_K_M"))
        self.assertGreater(spec.size_bytes, 2_600_000_000)
        self.assertLess(spec.size_bytes, 2_650_000_000)
        self.assertIn(Capability.THINKING, spec.capabilities)

    def test_every_entry_is_complete_and_local(self):
        specs = self.catalog.all()
        self.assertGreater(len(specs), 60)
        for spec in specs:
            with self.subTest(spec.name):
                self.assertGreater(spec.size_bytes, 0)
                self.assertTrue(spec.summary)
                self.assertTrue(spec.capabilities)
                self.assertTrue(spec.use_cases)
                self.assertFalse(spec.name.is_cloud)

    def test_covers_tiny_devices_and_big_servers(self):
        sizes = [spec.size_bytes for spec in self.catalog.all()]
        self.assertLess(min(sizes), 100_000_000)
        self.assertGreater(max(sizes), 100_000_000_000)

    def test_per_tag_capabilities_override_the_family(self):
        self.assertNotIn(Capability.VISION, self.catalog.find(AgentName("gemma3:1b")).capabilities)
        self.assertIn(Capability.VISION, self.catalog.find(AgentName("gemma3:4b")).capabilities)

    def test_data_is_deeply_immutable(self):
        spec = self.catalog.all()[0]
        self.assertIsInstance(spec.use_cases, tuple)
        hash(spec)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self.router.add("GET", "/a/{id}", lambda r: "one")
        self.router.add("POST", "/a/{id}/go", lambda r: "go")
        self.router.add("GET", "/m/{name+}/x", lambda r: "x")

    def test_params_are_decoded(self):
        self.assertEqual(self.router.resolve("GET", "/a/b%20c").params["id"], "b c")

    def test_single_segment_does_not_cross_slashes(self):
        with self.assertRaises(NotFound):
            self.router.resolve("GET", "/a/b/c")

    def test_greedy_segment_crosses_slashes(self):
        self.assertEqual(self.router.resolve("GET", "/m/a/b/c/x").params["name"], "a/b/c")

    def test_wrong_method(self):
        with self.assertRaises(MethodNotAllowed):
            self.router.resolve("DELETE", "/a/b")

    def test_regex_characters_in_templates_are_literal(self):
        self.router.add("GET", "/v1.0/x", lambda r: "x")
        with self.assertRaises(NotFound):
            self.router.resolve("GET", "/v1x0/x")


class CliTests(unittest.TestCase):
    AGENT = "qwen3:4b-q4_K_M"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name
        self.ollama = FakeOllama().__enter__()
        self.addCleanup(self.ollama.__exit__)

        original_init = Brain.__init__

        def patched(brain, settings):
            original_init(brain, settings)
            brain.hardware = FakeInspector(make_hardware(ram_gib=8))
            brain.registry = NoRegistry()

        patcher = mock.patch.object(Brain, "__init__", patched)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", self.data_dir, "--ollama-url", self.ollama.url, *args])
        return code, out.getvalue(), err.getvalue()

    def test_status(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("0.40.13", out)

    def test_hardware(self):
        code, out, _ = self.run_cli("hardware")
        self.assertEqual(code, 0)
        self.assertIn("Test CPU", out)
        self.assertIn("none detected", out)

    def test_agents_hides_impossible_ones_unless_asked(self):
        _, out, _ = self.run_cli("agents")
        self.assertIn("smollm2:135m", out)
        self.assertNotIn("qwen3:235b", out)
        self.assertIn("more cannot run", out)
        _, out, _ = self.run_cli("agents", "--all")
        self.assertIn("qwen3:235b", out)

    def test_lifecycle_and_chat(self):
        self.assertEqual(self.run_cli("install", self.AGENT)[0], 0)
        self.assertIn(self.AGENT, self.ollama.installed)
        code, out, _ = self.run_cli("chat", self.AGENT, "hi")
        self.assertEqual(code, 0)
        self.assertIn("Hello there!", out)
        self.assertIn(self.AGENT, self.ollama.loaded, "chat from the CLI mounts automatically")
        self.assertEqual(self.run_cli("unmount", self.AGENT)[0], 0)
        self.assertEqual(self.run_cli("mount", self.AGENT)[0], 0)
        self.assertEqual(self.run_cli("uninstall", self.AGENT)[0], 0)
        self.assertNotIn(self.AGENT, self.ollama.installed)

    def test_failures_are_friendly_with_a_nonzero_exit(self):
        code, out, err = self.run_cli("mount", self.AGENT)
        self.assertEqual(code, 1)
        self.assertIn("not installed", err)
        self.assertIn("Hint:", err)
        self.assertNotIn("Traceback", err)

    def test_cloud_is_refused(self):
        code, _, err = self.run_cli("install", "gpt-oss:120b-cloud")
        self.assertEqual(code, 1)
        self.assertIn("only runs agents locally", err)

    def test_token_is_stable(self):
        _, first, _ = self.run_cli("token")
        _, second, _ = self.run_cli("token")
        self.assertEqual(first, second)
        self.assertGreater(len(first.strip()), 30)

    def test_parser_suggests_a_correction(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["instal", "x"])
        self.assertIn("install", err.getvalue())


class NewPythonFeatureTests(unittest.TestCase):
    """Guards that the code keeps relying on 3.15 behaviour the way it is documented to work."""

    def test_lazy_import_keeps_startup_light(self):
        import subprocess
        import sys

        code = (
            "import sys, brain.interfaces.cli\n"
            "heavy = {'sqlite3', 'tarfile', 'ssl', 'http.client', 'urllib.request', 'subprocess'}\n"
            "print(sorted(heavy & set(sys.modules)))\n"
        )
        output = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent,
        )
        self.assertEqual(json.loads(output.stdout.replace("'", '"')), [])

    def test_catalog_json_stays_valid(self):
        path = Path(__file__).parent.parent / "brain/infrastructure/catalog.json"
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=frozendict, array_hook=tuple)
        self.assertIsInstance(document, frozendict)
        self.assertIsInstance(document["families"], tuple)

    def test_settings_is_frozen(self):
        settings = Settings()
        with self.assertRaises(AttributeError):
            settings.port = 1  # type: ignore[misc]
