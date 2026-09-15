"""Windows Service entry point for the headless Container Profiler Agent.

No pywin32 dependency is required. Runtime service control uses Advapi32 via
``profiler.core.windows_service``; installation helpers shell only to Windows'
built-in ``sc.exe``. Install/uninstall intentionally require an extra explicit
confirmation flag because they change persistent machine state.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import subprocess
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from profiler.core.agent_forwarding import AgentForwardingRuntime
from profiler.core.agent_openmetrics import AgentOpenMetricsWorker
from profiler.core.agent_runtime import LocalAgentProcess
from profiler.core.container_metrics import ContainerMetricsWorker
from profiler.core.statsd_metrics import StatsDMetricsWorker
from profiler.core.windows_service import WindowsServiceHost
from profiler.utils.paths import (
    get_forwarding_config_path,
    get_forwarding_db_path,
    get_telemetry_db_path,
)

SERVICE_NAME = "ContainerProfilerAgent"
DISPLAY_NAME = "Container Profiler Agent"
SERVICE_DESCRIPTION = "Native host and container observability agent for Container Profiler"


def build_service_binary_command(database: Path) -> str:
    """Return a correctly quoted Windows command line for SCM binPath."""
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "run", "--database", str(database)]
    else:
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--database",
            str(database),
        ]
    return subprocess.list2cmdline(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Container Profiler Windows Service host")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run under the Windows Service Control Manager")
    run.add_argument("--database", type=Path, default=get_telemetry_db_path())

    install = sub.add_parser("install", help="Install the Windows service using sc.exe")
    install.add_argument("--database", type=Path, default=get_telemetry_db_path())
    install.add_argument(
        "--confirm-install",
        action="store_true",
        help="Required acknowledgement that installation changes machine service state",
    )

    uninstall = sub.add_parser("uninstall", help="Delete the Windows service using sc.exe")
    uninstall.add_argument(
        "--confirm-uninstall",
        action="store_true",
        help="Required acknowledgement that the persistent service will be deleted",
    )

    sub.add_parser("start", help="Start the installed service")
    sub.add_parser("stop", help="Request the installed service to stop")
    sub.add_parser("query", help="Query current SCM service status")
    sub.add_parser(
        "print-install-command", help="Print the service binPath without changing the machine"
    ).add_argument("--database", type=Path, default=get_telemetry_db_path())
    return parser


def _windows_required() -> bool:
    if os.name == "nt":
        return True
    print("Windows service commands are only available on Windows.", file=sys.stderr)
    return False


def _run_sc(*args: str) -> int:
    completed = subprocess.run(
        ["sc.exe", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return int(completed.returncode)


def _service_runtime_kwargs() -> dict:
    """Compose the same default headless collectors used by the console Agent.

    Construction is hardware-safe: Docker clients remain lazy and the UDP
    socket/forwarder are not started until the service runtime starts.
    """
    return {
        "container_worker": ContainerMetricsWorker(interval_s=2.0, max_containers=256),
        "statsd_worker": StatsDMetricsWorker(
            host="127.0.0.1",
            port=8125,
            flush_interval_s=10.0,
        ),
        "openmetrics_worker": AgentOpenMetricsWorker(discovery_interval_s=10.0),
        "forwarding_worker": AgentForwardingRuntime(
            get_forwarding_config_path(),
            get_forwarding_db_path(),
            reload_interval_s=2.0,
        ),
    }


def _run_service(database: Path) -> int:
    if not _windows_required():
        return 2
    process = LocalAgentProcess(database.expanduser(), **_service_runtime_kwargs())
    host = WindowsServiceHost(SERVICE_NAME, process.run)
    return host.run()


def _install(database: Path, *, confirmed: bool) -> int:
    if not _windows_required():
        return 2
    if not confirmed:
        print(
            "Refusing to install without --confirm-install. This creates a persistent "
            "auto-start Windows service.",
            file=sys.stderr,
        )
        return 2
    command = build_service_binary_command(database.expanduser())
    code = _run_sc(
        "create",
        SERVICE_NAME,
        "binPath=",
        command,
        "start=",
        "auto",
        "DisplayName=",
        DISPLAY_NAME,
    )
    if code != 0:
        return code
    description_code = _run_sc("description", SERVICE_NAME, SERVICE_DESCRIPTION)
    if description_code != 0:
        print(
            "Service was created but its description could not be set; installation remains active.",
            file=sys.stderr,
        )
    return description_code


def _uninstall(*, confirmed: bool) -> int:
    if not _windows_required():
        return 2
    if not confirmed:
        print(
            "Refusing to delete the service without --confirm-uninstall.",
            file=sys.stderr,
        )
        return 2
    return _run_sc("delete", SERVICE_NAME)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run_service(args.database)
    if args.command == "install":
        return _install(args.database, confirmed=args.confirm_install)
    if args.command == "uninstall":
        return _uninstall(confirmed=args.confirm_uninstall)
    if args.command == "print-install-command":
        print(build_service_binary_command(args.database.expanduser()))
        return 0
    if not _windows_required():
        return 2
    if args.command == "start":
        return _run_sc("start", SERVICE_NAME)
    if args.command == "stop":
        return _run_sc("stop", SERVICE_NAME)
    if args.command == "query":
        return _run_sc("query", SERVICE_NAME)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
