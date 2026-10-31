"""Ways to get Ollama onto a machine.

The official script is the best option, but it needs root and a web button cannot type a sudo
password. The rootless installer therefore covers everything else, including most containers.
"""

lazy import importlib.util
lazy import shutil
lazy import subprocess
lazy import tarfile
lazy import tempfile
lazy import urllib.error
lazy import urllib.request

import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from brain.domain.errors import InstallFailed
from brain.ports import ProgressSink, RuntimeInstaller

_DOWNLOAD_BASE = "https://ollama.com/download"
_SCRIPT_URL = "https://ollama.com/install.sh"
_ARCHIVES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_CHUNK_BYTES = 1024 * 1024
_NETWORK_TIMEOUT_SECONDS = 60
# Measured: the 1.5 GB amd64 archive unpacks to 2.6 GB, and both exist at once while unpacking.
_PEAK_DISK_FACTOR = 3
_ESCALATION_CHECK_SECONDS = 300.0
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SCRIPT_OUTPUT_SEPARATOR = re.compile(r"[\r\n]+")


def _can_escalate_without_prompt() -> bool:
    if os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        return False
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, timeout=5, check=False
            ).returncode
            == 0
        )
    except OSError, subprocess.SubprocessError:
        return False


def _download(url: str, destination: Path, progress: ProgressSink, label: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "local-ai-brain"})
    try:
        with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_SECONDS) as response:
            total = int(response.headers.get("Content-Length") or 0) or None
            completed = 0
            with destination.open("wb") as file:
                while chunk := response.read(_CHUNK_BYTES):
                    progress.raise_if_cancelled()
                    file.write(chunk)
                    completed += len(chunk)
                    progress.report(label, completed=completed, total=total)
    except urllib.error.HTTPError as error:
        error.close()
        raise InstallFailed(f"Downloading {url} failed with HTTP {error.code}.") from error
    except OSError as error:
        raise InstallFailed(f"Downloading {url} failed: {error}") from error


class OfficialScriptInstaller:
    """Ollama's own installer: system-wide, with a systemd service and GPU drivers."""

    method = "official"
    label = "Official installer (system-wide service)"

    def __init__(
        self,
        *,
        script_url: str = _SCRIPT_URL,
        machine: Callable[[], str] = lambda: os.uname().machine,
        can_escalate: Callable[[], bool] = _can_escalate_without_prompt,
    ) -> None:
        self._script_url = script_url
        self._machine = machine
        self._can_escalate = can_escalate
        self._escalation_answer: bool | None = None
        self._escalation_expires = 0.0

    def availability(self) -> tuple[bool, str]:
        if self._machine() not in _ARCHIVES:
            return False, f"Ollama does not support the {self._machine()} architecture."
        if shutil.which("curl") is None:
            return False, "The official script needs curl, which is not installed."
        if not self._may_escalate():
            return False, "Needs root or passwordless sudo, as the dashboard cannot ask for a password."
        return True, "Installs a system service and any GPU support it detects."

    def _may_escalate(self) -> bool:
        """Remembers the answer, as every failed `sudo -n` adds a line to the system's auth log."""
        now = time.monotonic()
        if self._escalation_answer is None or now >= self._escalation_expires:
            self._escalation_answer = self._can_escalate()
            self._escalation_expires = now + _ESCALATION_CHECK_SECONDS
        return self._escalation_answer

    def install(self, progress: ProgressSink) -> None:
        with tempfile.TemporaryDirectory(prefix="brain-ollama-") as directory:
            script = Path(directory) / "install.sh"
            _download(self._script_url, script, progress, "Fetching the official installer")
            progress.report("Running the official installer", completed=None, total=None)
            self._run(script, progress)

    @staticmethod
    def _run(script: Path, progress: ProgressSink) -> None:
        process = subprocess.Popen(
            ["sh", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        tail = ""
        try:
            while chunk := process.stdout.read1(4096):
                progress.raise_if_cancelled()
                text = _ANSI_ESCAPE.sub("", chunk.decode("utf-8", "replace"))
                lines = [line for line in _SCRIPT_OUTPUT_SEPARATOR.split(text) if line.strip()]
                if lines:
                    tail = lines[-1].strip()
                    progress.report(tail)
        except BaseException:
            process.terminate()
            raise
        finally:
            process.stdout.close()
        if process.wait() != 0:
            raise InstallFailed(f"The official installer failed: {tail or 'no output'}")


class RootlessInstaller:
    """Unpacks Ollama's release archive into a private folder. Needs no root and no systemd."""

    method = "rootless"
    label = "Rootless install (this user only)"

    def __init__(
        self,
        install_dir: Path,
        *,
        base_url: str = _DOWNLOAD_BASE,
        machine: Callable[[], str] = lambda: os.uname().machine,
        extras: Callable[[], Sequence[str]] = lambda: (),
    ) -> None:
        self._install_dir = install_dir
        self._base_url = base_url
        self._machine = machine
        self._extras = extras

    @property
    def binary(self) -> Path:
        return self._install_dir / "bin/ollama"

    def availability(self) -> tuple[bool, str]:
        if self._machine() not in _ARCHIVES:
            return False, f"Ollama does not support the {self._machine()} architecture."
        if not self._has_zstd_support():
            return False, "Needs the zstd tool to unpack Ollama. Install the 'zstd' package."
        return True, f"Installs into {self._install_dir}, without changing the rest of the system."

    def install(self, progress: ProgressSink) -> None:
        architecture = _ARCHIVES.get(self._machine())
        if architecture is None:
            raise InstallFailed(f"Ollama does not support the {self._machine()} architecture.")
        if not self._has_zstd_support():
            raise InstallFailed("Unpacking Ollama needs the zstd tool, which is not installed.")

        self._install_dir.parent.mkdir(parents=True, exist_ok=True, parent_mode=0o755)
        staging = self._install_dir.with_name(f"{self._install_dir.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o755)
        try:
            for suffix in ("", *self._extras()):
                archive = f"ollama-linux-{architecture}{suffix}.tar.zst"
                self._fetch_and_unpack(archive, staging, progress)
            self._verify(staging / "bin/ollama", progress)
            progress.report("Finishing up", completed=None, total=None)
            # Swapping the folder in last means an interrupted install is never mistaken for a good one.
            shutil.rmtree(self._install_dir, ignore_errors=True)
            staging.rename(self._install_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _fetch_and_unpack(self, archive: str, staging: Path, progress: ProgressSink) -> None:
        url = f"{self._base_url}/{archive}"
        download = staging.parent / f"{archive}.part"
        try:
            self._check_disk_space(url, staging)
            _download(url, download, progress, f"Downloading {archive}")
            progress.raise_if_cancelled()
            progress.report(f"Unpacking {archive}", completed=None, total=None)
            self._unpack(download, staging)
        finally:
            download.unlink(missing_ok=True)

    @staticmethod
    def _check_disk_space(url: str, staging: Path) -> None:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "local-ai-brain"})
        try:
            with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_SECONDS) as response:
                size = int(response.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as error:
            error.close()
            return  # The download that follows will report the real problem.
        except OSError:
            return
        needed = size * _PEAK_DISK_FACTOR
        free = shutil.disk_usage(staging).free
        if needed > free:
            raise InstallFailed(
                f"Installing Ollama needs about {needed / 1e9:.1f} GB of disk, "
                f"but only {free / 1e9:.1f} GB is free."
            )

    @staticmethod
    def _has_zstd_support() -> bool:
        return bool(shutil.which("zstd")) or importlib.util.find_spec("_zstd") is not None

    @classmethod
    def _unpack(cls, archive: Path, destination: Path) -> None:
        """`filter="data"` refuses archive members that try to escape the destination folder."""
        try:
            if importlib.util.find_spec("_zstd") is not None:
                with tarfile.open(archive, mode="r:zst") as tar:
                    tar.extractall(destination, filter="data")
                return
            cls._unpack_with_zstd_tool(archive, destination)
        except (OSError, tarfile.TarError) as error:
            raise InstallFailed(f"Could not unpack {archive.name}: {error}") from error

    @staticmethod
    def _unpack_with_zstd_tool(archive: Path, destination: Path) -> None:
        """Many Python builds, including some 3.15 ones, are compiled without zstd support."""
        process = subprocess.Popen(
            ["zstd", "--decompress", "--stdout", "--quiet", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
                tar.extractall(destination, filter="data")
        finally:
            process.stdout.close()
            errors = process.stderr.read()
            process.stderr.close()
            code = process.wait()
        if code != 0:
            message = errors.decode("utf-8", "replace").strip()
            raise InstallFailed(f"zstd could not read {archive.name}: {message}")

    @staticmethod
    def _verify(binary: Path, progress: ProgressSink) -> None:
        progress.report("Checking the new Ollama binary", completed=None, total=None)
        if not binary.is_file():
            raise InstallFailed("The Ollama archive did not contain bin/ollama.")
        try:
            subprocess.run(
                [str(binary), "--version"], capture_output=True, timeout=30, check=True
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallFailed(f"The downloaded Ollama binary does not run: {error}") from error


class GpuArchiveSelector:
    """Picks extra archives that the base Ollama download leaves out."""

    def __init__(self, root: Path = Path("/")) -> None:
        self._root = root

    def __call__(self) -> Sequence[str]:
        extras: list[str] = []
        tegra_release = self._root / "etc/nv_tegra_release"
        if tegra_release.exists():
            text = tegra_release.read_text(encoding="utf-8", errors="replace")
            if "R36" in text:
                extras.append("-jetpack6")
            elif "R35" in text:
                extras.append("-jetpack5")
        elif any(
            path.read_text(encoding="utf-8", errors="replace").strip() == "0x1002"
            for path in (self._root / "sys/class/drm").glob("card*/device/vendor")
        ):
            extras.append("-rocm")
        return extras


def pick_installer(
    installers: Sequence[RuntimeInstaller], method: str | None = None
) -> RuntimeInstaller:
    """The first usable installer wins, so the list order states the preference."""
    for installer in installers:
        if method is not None and installer.method != method:
            continue
        usable, reason = installer.availability()
        if usable:
            return installer
        if method is not None:
            raise InstallFailed(f"{installer.label} cannot run here: {reason}")
    if method is not None:
        raise InstallFailed(f"Unknown install method '{method}'.")
    reasons = "; ".join(installer.availability()[1] for installer in installers)
    raise InstallFailed(f"No install method can run on this machine: {reasons}")
