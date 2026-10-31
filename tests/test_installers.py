"""Install logic, against a local server handing out real .tar.zst archives."""

import http.server
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from brain.domain.errors import InstallFailed, JobCancelled, RuntimeUnavailable
from brain.infrastructure.ollama.installers import (
    GpuArchiveSelector,
    OfficialScriptInstaller,
    RootlessInstaller,
    pick_installer,
)
from brain.infrastructure.ollama.supervisor import OllamaSupervisor, is_loopback_url
from tests.fakes import SilentSink, write_tree

FAKE_BINARY = "#!/bin/sh\necho 'ollama version is 9.9.9'\n"
HAS_ZSTD = shutil.which("zstd") is not None


def make_archive(directory: Path, name: str, members: dict[str, str]) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    subprocess.run(
        ["zstd", "-q", "-f", "-o", str(directory / name)], input=buffer.getvalue(), check=True
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


class _Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.site = self.tmp / "site"
        self.site.mkdir()
        handler = partial(_QuietHandler, directory=str(self.site))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.install_dir = self.tmp / "data/ollama"

    def installer(self, **kwargs) -> RootlessInstaller:
        kwargs.setdefault("machine", lambda: "x86_64")
        return RootlessInstaller(self.install_dir, base_url=self.base_url, **kwargs)


@unittest.skipUnless(HAS_ZSTD, "needs the zstd tool")
class RootlessInstallTests(_Fixture):
    def test_installs_a_working_binary(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"bin/ollama": FAKE_BINARY, "lib/ollama/x.so": "lib"})
        sink = SilentSink()
        self.installer().install(sink)
        binary = self.install_dir / "bin/ollama"
        self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)
        self.assertEqual((self.install_dir / "lib/ollama/x.so").read_text(), "lib")
        self.assertTrue(any("Downloading" in message for message in sink.messages))
        self.assertEqual([p.name for p in self.install_dir.parent.iterdir()], ["ollama"])

    def test_picks_the_arm64_archive_on_a_raspberry_pi(self):
        make_archive(self.site, "ollama-linux-arm64.tar.zst", {"bin/ollama": FAKE_BINARY})
        self.installer(machine=lambda: "aarch64").install(SilentSink())
        self.assertTrue((self.install_dir / "bin/ollama").exists())

    def test_extra_gpu_archives_are_added(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"bin/ollama": FAKE_BINARY})
        make_archive(self.site, "ollama-linux-amd64-rocm.tar.zst", {"lib/ollama/rocm.so": "rocm"})
        self.installer(extras=lambda: ("-rocm",)).install(SilentSink())
        self.assertEqual((self.install_dir / "lib/ollama/rocm.so").read_text(), "rocm")

    def test_a_malicious_archive_cannot_escape_the_install_folder(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"bin/ollama": FAKE_BINARY, "../../escaped": "x"})
        with self.assertRaises(InstallFailed):
            self.installer().install(SilentSink())
        self.assertFalse((self.tmp / "escaped").exists())
        self.assertFalse((self.tmp / "data/escaped").exists())
        self.assertFalse(self.install_dir.exists())

    def test_a_failed_install_leaves_the_previous_one_untouched(self):
        write_tree(self.install_dir, {"bin/ollama": "previous"})
        with self.assertRaises(InstallFailed):
            self.installer().install(SilentSink())  # The archive is missing, so this is a 404.
        self.assertEqual((self.install_dir / "bin/ollama").read_text(), "previous")
        self.assertFalse(self.install_dir.with_name("ollama.staging").exists())

    def test_archive_without_the_binary_fails(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"readme.txt": "nope"})
        with self.assertRaises(InstallFailed) as caught:
            self.installer().install(SilentSink())
        self.assertIn("bin/ollama", caught.exception.message)
        self.assertFalse(self.install_dir.exists())

    def test_binary_that_does_not_run_fails(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"bin/ollama": "#!/bin/sh\nexit 3\n"})
        with self.assertRaises(InstallFailed):
            self.installer().install(SilentSink())
        self.assertFalse(self.install_dir.exists())

    def test_corrupt_archive_fails_cleanly(self):
        (self.site / "ollama-linux-amd64.tar.zst").write_bytes(b"this is not zstd data")
        with self.assertRaises(InstallFailed):
            self.installer().install(SilentSink())
        self.assertFalse(self.install_dir.exists())
        self.assertEqual(list(self.install_dir.parent.iterdir()), [])

    def test_cancelling_cleans_up(self):
        make_archive(self.site, "ollama-linux-amd64.tar.zst", {"bin/ollama": FAKE_BINARY})
        sink = SilentSink()
        sink.cancelled = True
        with self.assertRaises(JobCancelled):
            self.installer().install(sink)
        self.assertEqual(list(self.install_dir.parent.iterdir()), [])


class AvailabilityTests(_Fixture):
    def test_rootless_refuses_32_bit_arm(self):
        usable, reason = self.installer(machine=lambda: "armv7l").availability()
        self.assertFalse(usable)
        self.assertIn("armv7l", reason)
        with self.assertRaises(InstallFailed):
            self.installer(machine=lambda: "armv7l").install(SilentSink())

    def test_official_needs_a_way_to_become_root(self):
        installer = OfficialScriptInstaller(machine=lambda: "x86_64", can_escalate=lambda: False)
        if shutil.which("curl"):
            usable, reason = installer.availability()
            self.assertFalse(usable)
            self.assertIn("sudo", reason)

    def test_official_usable_with_passwordless_sudo(self):
        installer = OfficialScriptInstaller(machine=lambda: "x86_64", can_escalate=lambda: True)
        if shutil.which("curl"):
            self.assertTrue(installer.availability()[0])


class _StubInstaller:
    def __init__(self, method, usable, reason="why"):
        self.method = method
        self.label = method
        self._usable = usable
        self._reason = reason

    def availability(self):
        return self._usable, self._reason


class PickInstallerTests(unittest.TestCase):
    def test_first_usable_wins(self):
        chosen = pick_installer([_StubInstaller("official", False), _StubInstaller("rootless", True)])
        self.assertEqual(chosen.method, "rootless")

    def test_named_method_is_respected(self):
        chosen = pick_installer([_StubInstaller("official", True), _StubInstaller("rootless", True)], "rootless")
        self.assertEqual(chosen.method, "rootless")

    def test_named_but_unusable_explains_why(self):
        with self.assertRaises(InstallFailed) as caught:
            pick_installer([_StubInstaller("official", False, "needs root")], "official")
        self.assertIn("needs root", caught.exception.message)

    def test_unknown_method(self):
        with self.assertRaises(InstallFailed):
            pick_installer([_StubInstaller("official", True)], "magic")

    def test_nothing_usable_lists_every_reason(self):
        with self.assertRaises(InstallFailed) as caught:
            pick_installer([_StubInstaller("a", False, "no root"), _StubInstaller("b", False, "no zstd")])
        self.assertIn("no root", caught.exception.message)
        self.assertIn("no zstd", caught.exception.message)


class GpuArchiveSelectorTests(_Fixture):
    def test_plain_machine_needs_nothing_extra(self):
        self.assertEqual(GpuArchiveSelector(self.tmp)(), [])

    def test_amd_card_adds_rocm(self):
        write_tree(self.tmp, {"sys/class/drm/card0/device/vendor": "0x1002\n"})
        self.assertEqual(GpuArchiveSelector(self.tmp)(), ["-rocm"])

    def test_jetson_versions(self):
        write_tree(self.tmp, {"etc/nv_tegra_release": "# R36 (release)"})
        self.assertEqual(GpuArchiveSelector(self.tmp)(), ["-jetpack6"])
        write_tree(self.tmp, {"etc/nv_tegra_release": "# R35 (release)"})
        self.assertEqual(GpuArchiveSelector(self.tmp)(), ["-jetpack5"])


class SupervisorTests(_Fixture):
    def supervisor(self, *, reachable=lambda: False, binary: Path | None = None, url="http://127.0.0.1:1", timeout=5.0):
        return OllamaSupervisor(
            base_url=url,
            rootless_binary=binary or self.tmp / "missing",
            log_path=self.tmp / "ollama.log",
            is_reachable=reachable,
            environ={"PATH": os.environ["PATH"]},
            start_timeout=timeout,
        )

    def test_loopback_detection(self):
        self.assertTrue(is_loopback_url("http://127.0.0.1:11434"))
        self.assertTrue(is_loopback_url("http://localhost:11434"))
        self.assertFalse(is_loopback_url("http://ollama:11434"))

    def test_already_running_does_nothing(self):
        supervisor = self.supervisor(reachable=lambda: True)
        supervisor.start()
        self.assertFalse(supervisor.is_managed())

    def test_remote_runtime_cannot_be_started(self):
        with self.assertRaises(RuntimeUnavailable):
            self.supervisor(url="http://10.0.0.5:11434").start()

    def _fake_binary(self, body: str) -> Path:
        path = self.tmp / "bin-ollama"
        path.write_text(body)
        path.chmod(0o755)
        return path

    def _spawn_only(self, supervisor: OllamaSupervisor) -> OllamaSupervisor:
        supervisor._start_system_service = lambda: False
        supervisor.binary_path = lambda: str(supervisor._rootless_binary)
        return supervisor

    def test_spawns_and_then_stops_its_own_child(self):
        binary = self._fake_binary("#!/bin/sh\nexec sleep 60\n")
        marks = iter([False, True, True])
        supervisor = self._spawn_only(self.supervisor(reachable=lambda: next(marks), binary=binary))
        supervisor.start()
        self.assertTrue(supervisor.is_managed())
        child = supervisor._child
        supervisor.stop()
        self.assertFalse(supervisor.is_managed())
        self.assertIsNotNone(child.poll())

    def test_child_gets_the_right_host_and_cloud_is_turned_off(self):
        marker = self.tmp / "env.txt"
        binary = self._fake_binary(f'#!/bin/sh\nenv > "{marker}"\nexec sleep 60\n')

        def reachable():
            return marker.exists() and marker.stat().st_size > 0

        supervisor = self._spawn_only(
            self.supervisor(reachable=reachable, binary=binary, url="http://127.0.0.1:11500")
        )
        supervisor.start()
        self.addCleanup(supervisor.stop)
        environment = marker.read_text()
        self.assertIn("OLLAMA_HOST=127.0.0.1:11500", environment)
        self.assertIn("OLLAMA_NO_CLOUD=1", environment)

    def test_child_that_dies_is_reported_with_its_exit_code(self):
        binary = self._fake_binary("#!/bin/sh\nexit 7\n")
        supervisor = self._spawn_only(self.supervisor(binary=binary))
        with self.assertRaises(RuntimeUnavailable) as caught:
            supervisor.start()
        self.assertIn("7", caught.exception.message)
        self.assertIn("ollama.log", caught.exception.hint)

    def test_never_answering_times_out(self):
        binary = self._fake_binary("#!/bin/sh\nexec sleep 60\n")
        supervisor = self._spawn_only(self.supervisor(binary=binary, timeout=0.3))
        self.addCleanup(supervisor.stop)
        with self.assertRaises(RuntimeUnavailable):
            supervisor.start()

    def test_missing_binary(self):
        supervisor = self.supervisor()
        supervisor.binary_path = lambda: None
        with self.assertRaises(RuntimeUnavailable) as caught:
            supervisor.start()
        self.assertIn("not installed", caught.exception.message)

    def test_finds_rootless_binary_when_not_on_path(self):
        binary = self._fake_binary("")
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = "/nonexistent"
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        self.assertEqual(self.supervisor(binary=binary).binary_path(), str(binary))
