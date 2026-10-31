"""The `brain` command. Each subcommand is a small class, so adding one touches nothing else."""

lazy import signal
lazy import threading
lazy import uuid
lazy from brain.interfaces.http.app import build_server

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from brain import __version__
from brain.config import DEFAULT_PORT, SettingsLoader, TokenStore
from brain.container import Brain
from brain.domain.agents import Agent
from brain.domain.compatibility import Severity, Verdict, format_bytes
from brain.domain.conversation import ChatEventType, ChatRequest
from brain.domain.errors import BrainError
from brain.domain.jobs import Job, JobState

_VERDICT_LABELS = {
    Verdict.RUNS_WELL: "runs well",
    Verdict.LIMITED: "limited",
    Verdict.UNAVAILABLE: "not here",
    Verdict.UNKNOWN: "unknown",
}
_PROGRESS_INTERVAL_SECONDS = 0.5


class Command(Protocol):
    name: str
    help: str

    def configure(self, parser: argparse.ArgumentParser) -> None: ...

    def run(self, brain: Brain, args: argparse.Namespace) -> int: ...


def _print_table(rows: Sequence[Sequence[str]], *, headers: Sequence[str]) -> None:
    table = [list(headers), *[list(row) for row in rows]]
    widths = [max(len(row[column]) for row in table) for column in range(len(headers))]
    for index, row in enumerate(table):
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))


def _follow_job(brain: Brain, job: Job) -> Job:
    """Shows progress on the terminal until the job finishes."""
    interactive = sys.stderr.isatty()
    last_line = ""
    while True:
        current = brain.jobs.wait(job.id, _PROGRESS_INTERVAL_SECONDS)
        line = current.message
        if current.total:
            percent = 100 * (current.completed or 0) / current.total
            line = f"{current.message}  {percent:5.1f}% of {format_bytes(current.total)}"
        if line and line != last_line:
            print(f"\r\033[K{line}" if interactive else line, end="" if interactive else "\n",
                  file=sys.stderr, flush=True)
            last_line = line
        if not current.is_active:
            if interactive and last_line:
                print(file=sys.stderr)
            return current


def _finish(job: Job, success: str) -> int:
    match job.state:
        case JobState.SUCCEEDED:
            print(success)
            return 0
        case JobState.CANCELLED:
            print("Cancelled.", file=sys.stderr)
            return 1
        case _:
            print(f"Failed: {job.error}", file=sys.stderr)
            return 1


class ServeCommand:
    name = "serve"
    help = "Start the brain: the dashboard and the API."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--host", help="Address to listen on. Default: 127.0.0.1")
        parser.add_argument("--port", type=int, help=f"Port to listen on. Default: {DEFAULT_PORT}")
        parser.add_argument(
            "--auto-install",
            action="store_true",
            default=None,
            dest="auto_install_ollama",
            help="Install Ollama at startup if it is missing, for unattended setups.",
        )

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        settings = brain.settings
        try:
            server = build_server(brain)
        except OSError as error:
            raise BrainError(
                f"Cannot listen on {settings.host}:{settings.port}: {error.strerror}.",
                hint="Is another brain already running? Choose a different one with --port.",
            ) from error
        host, port = server.server_address[:2]
        self._welcome(brain, str(host), int(port))

        self._prepare_runtime(brain)

        stop = threading.Event()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, lambda *_: stop.set())
        worker = threading.Thread(target=server.serve_forever, name="http", daemon=True)
        worker.start()
        try:
            stop.wait()
        finally:
            print("\nStopping.", file=sys.stderr)
            server.shutdown()
            server.server_close()
            brain.runtime.shutdown()
        return 0

    @staticmethod
    def _prepare_runtime(brain: Brain) -> None:
        settings = brain.settings

        def prepare() -> None:
            if settings.auto_start_ollama or settings.auto_install_ollama:
                try:
                    job = brain.runtime.ensure_ready(allow_install=settings.auto_install_ollama)
                    if job is not None:
                        brain.runtime.wait(job)
                except BrainError as error:
                    logging.getLogger("brain").warning("Ollama: %s", error.message)
            if settings.restore_mounts:
                brain.agents.restore_mounts()

        threading.Thread(target=prepare, name="startup", daemon=True).start()

    @staticmethod
    def _welcome(brain: Brain, host: str, port: int) -> None:
        settings = brain.settings
        visible_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        print(f"Local AI Brain {__version__}")
        print(f"  Dashboard  http://{visible_host}:{port}")
        print(f"  API        http://{visible_host}:{port}/api/v1")
        print(f"  Ollama     {settings.ollama_url}")
        if brain.token:
            print(f"  Token file {TokenStore(settings.data_dir).path}")
            print("  Run `brain token` to see the API token for other machines.")
        elif not settings.is_loopback_only:
            print("  WARNING: listening on the network with authentication turned off.")
        sys.stdout.flush()


class StatusCommand:
    name = "status"
    help = "Show whether Ollama is installed and running."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        status = brain.runtime.status()
        print(f"Ollama URL    {status.url}")
        print(f"Installed     {'yes' if status.installed else 'no'}")
        print(f"Running       {'yes' if status.reachable else 'no'}")
        print(f"Version       {status.version or '-'}")
        print(f"Binary        {status.binary_path or '-'}")
        if not status.installed:
            for option in status.installers:
                marker = "available" if option.usable else "unavailable"
                print(f"Install via   {option.method} ({marker}): {option.reason}")
        return 0 if status.reachable else 1


class HardwareCommand:
    name = "hardware"
    help = "Show what this machine can do."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        profile = brain.hardware.snapshot()
        environment, cpu, memory, disk = (
            profile.environment, profile.cpu, profile.memory, profile.disk,
        )
        print(f"Host      {environment.hostname} ({environment.deployment.value})")
        print(f"System    {environment.operating_system}, kernel {environment.kernel}")
        if environment.board:
            print(f"Board     {environment.board}")
        print(f"CPU       {cpu.model}, {cpu.effective_cores:g} of {cpu.logical_cores} cores, "
              f"{cpu.architecture}")
        limit = " (container limit)" if memory.limited_by_cgroup else ""
        print(f"Memory    {format_bytes(memory.available_bytes)} free of "
              f"{format_bytes(memory.total_bytes)}{limit}")
        print(f"Disk      {format_bytes(disk.free_bytes)} free of "
              f"{format_bytes(disk.total_bytes)} at {disk.path}")
        if not profile.gpus:
            print("GPU       none detected")
        for gpu in profile.gpus:
            usable = "" if gpu.supported else f" (not usable: {gpu.note})"
            print(f"GPU       {gpu.name}, {format_bytes(gpu.vram_total_bytes)}{usable}")
        for sensor in profile.sensors:
            print(f"Sensor    {sensor.label}: {sensor.value:.1f} {sensor.unit}")
        for peripheral in profile.peripherals:
            print(f"Device    {peripheral.kind}: {peripheral.path}")
        return 0


class AgentsCommand:
    name = "agents"
    help = "List agents and whether each can run on this machine."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--all", action="store_true", help="Include agents too big to run here.")

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        overview = brain.agents.overview()
        if not overview.runtime_online:
            print("Ollama is not running, so installed agents cannot be shown.\n", file=sys.stderr)
        rows = [
            (
                agent.name,
                agent.state.value,
                _VERDICT_LABELS[agent.compatibility.verdict],
                format_bytes(agent.size_bytes) if agent.size_bytes is not None else "?",
                ", ".join(sorted(agent.capabilities)),
                self._reason(agent),
            )
            for agent in overview.agents
            if args.all or agent.compatibility.available
        ]
        _print_table(rows, headers=("AGENT", "STATE", "HERE", "SIZE", "CAN DO", "NOTE"))
        hidden = len(overview.agents) - len(rows)
        if hidden:
            print(f"\n{hidden} more cannot run on this machine. Use --all to see them.")
        return 0

    @staticmethod
    def _reason(agent: Agent) -> str:
        worst = max(
            agent.compatibility.findings, key=lambda finding: finding.severity, default=None
        )
        return worst.message if worst and worst.severity > Severity.INFO else ""


class _AgentCommand:
    """Shared shape of the lifecycle commands, which all take one agent name."""

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent", help="For example `qwen3:4b-q4_K_M`.")


class InstallCommand(_AgentCommand):
    name = "install"
    help = "Download an agent."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        parser.add_argument("--force", action="store_true", help="Ignore compatibility checks.")

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        job = _follow_job(brain, brain.agents.install(args.agent, force=args.force))
        return _finish(job, f"Installed {args.agent}.")


class MountCommand(_AgentCommand):
    name = "mount"
    help = "Load an agent into memory and keep it there."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        parser.add_argument("--force", action="store_true", help="Ignore compatibility checks.")

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        job = _follow_job(brain, brain.agents.mount(args.agent, force=args.force))
        return _finish(job, f"Mounted {args.agent}.")


class UnmountCommand(_AgentCommand):
    name = "unmount"
    help = "Release an agent from memory."

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        job = _follow_job(brain, brain.agents.unmount(args.agent))
        return _finish(job, f"Unmounted {args.agent}.")


class UninstallCommand(_AgentCommand):
    name = "uninstall"
    help = "Delete an agent's files."

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        job = _follow_job(brain, brain.agents.uninstall(args.agent))
        return _finish(job, f"Uninstalled {args.agent}.")


class ChatCommand(_AgentCommand):
    name = "chat"
    help = "Talk to an agent. Give a message for one reply, or none for a conversation."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        parser.add_argument("message", nargs="?", help="Omit to chat interactively.")
        parser.add_argument("--system", help="Instructions that shape the agent's behaviour.")
        parser.add_argument("--think", action="store_true", help="Let the agent reason first.")
        parser.add_argument("--conversation", help="Keep history under this name.")
        parser.add_argument(
            "--sensors", action="store_true", help="Tell the agent the latest sensor readings."
        )

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        if args.message is not None:
            return self._turn(brain, args, args.message, args.conversation)
        return self._conversation(brain, args)

    def _conversation(self, brain: Brain, args: argparse.Namespace) -> int:
        conversation = args.conversation or f"cli-{uuid.uuid7()}"
        print(f"Talking to {args.agent}. Press Ctrl+D to leave.", file=sys.stderr)
        while True:
            try:
                message = input("\nyou> ").strip()
            except EOFError, KeyboardInterrupt:
                print()
                return 0
            if message:
                try:
                    self._turn(brain, args, message, conversation)
                except KeyboardInterrupt:
                    print("\n[stopped]", file=sys.stderr)

    @staticmethod
    def _turn(brain: Brain, args: argparse.Namespace, message: str, conversation: str | None) -> int:
        fields = {"system": args.system} if args.system else {}
        request = ChatRequest(
            agent=args.agent,
            message=message,
            conversation_id=conversation,
            think=args.think,
            auto_mount=True,
            sensor_context=args.sensors,
            **fields,
        )
        for event in brain.chat.stream(request):
            match event.type:
                case ChatEventType.THINKING:
                    print(f"\033[2m{event.text}\033[0m", end="", file=sys.stderr, flush=True)
                case ChatEventType.CONTENT:
                    print(event.text, end="", flush=True)
                case ChatEventType.ERROR:
                    print(f"\nFailed: {(event.error or {}).get('message')}", file=sys.stderr)
                    return 1
        print()
        return 0


class OllamaCommand:
    name = "ollama"
    help = "Install or start Ollama itself."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("action", choices=("install", "start"))
        parser.add_argument("--method", help="`official` or `rootless`. Default: best available.")

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        if args.action == "install":
            job = _follow_job(brain, brain.runtime.install(method=args.method))
            code = _finish(job, "Ollama is installed and running.")
        else:
            job = _follow_job(brain, brain.runtime.start())
            code = _finish(job, "Ollama is running.")
        if code == 0 and brain.supervisor.is_managed():
            print(
                "Note: this Ollama stops when this command exits. "
                "`brain serve` starts and keeps its own."
            )
            brain.runtime.shutdown()
        return code


class TokenCommand:
    name = "token"
    help = "Print the API token that other machines must send."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, brain: Brain, args: argparse.Namespace) -> int:
        settings = brain.settings
        print(settings.token or TokenStore(settings.data_dir).load_or_create())
        return 0


COMMANDS: tuple[Command, ...] = (
    ServeCommand(),
    StatusCommand(),
    HardwareCommand(),
    AgentsCommand(),
    InstallCommand(),
    MountCommand(),
    UnmountCommand(),
    UninstallCommand(),
    ChatCommand(),
    OllamaCommand(),
    TokenCommand(),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain",
        description="Run, manage and talk to local AI agents on any Linux machine.",
    )
    parser.add_argument("--version", action="version", version=f"brain {__version__}")
    parser.add_argument("--config", type=Path, help="Path to a `brain.toml` settings file.")
    parser.add_argument("--ollama-url", help="Where Ollama listens, if not on this machine.")
    parser.add_argument("--data-dir", type=Path, help="Where the brain keeps its data.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logging.")
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)
    for command in COMMANDS:
        command.configure(commands.add_parser(command.name, help=command.help, description=command.help))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    command = next(command for command in COMMANDS if command.name == args.command)
    try:
        overrides = {
            key: getattr(args, key, None)
            for key in ("host", "port", "ollama_url", "data_dir", "auto_install_ollama")
        }
        brain = Brain(SettingsLoader(config_path=args.config).load(overrides))
        try:
            return command.run(brain, args)
        finally:
            brain.close()
    except BrainError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        if error.hint:
            print(f"Hint: {error.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
