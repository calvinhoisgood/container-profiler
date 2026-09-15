"""Headless Container Profiler Agent entry point.

This process intentionally imports no Qt modules. It is suitable for foreground
operation today and is the runtime that future Windows Service/systemd wrappers
will host instead of keeping collection tied to the desktop Explorer.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.agent_status import read_agent_status, write_agent_status
from profiler.core.storage import SQLiteTelemetryStore
from profiler.utils.paths import get_agent_status_path, get_telemetry_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Container Profiler headless agent")
    parser.add_argument(
        "--database",
        type=Path,
        default=get_telemetry_db_path(),
        help="SQLite telemetry database path",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Persistence drain interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=get_agent_status_path(),
        help="Atomic heartbeat/status JSON path",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=5.0,
        help="Status heartbeat interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current headless-agent status and exit",
    )
    parser.add_argument(
        "--status-max-age",
        type=float,
        default=15.0,
        help="Maximum heartbeat age considered healthy by --status",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, frame) -> None:
        del frame
        logging.getLogger("profiler.agent").info("shutdown requested by signal %s", signum)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, request_stop)
        except (OSError, ValueError):
            # Some embedded/service contexts do not permit all signal handlers.
            continue


def _print_status(path: Path, *, max_age_s: float) -> int:
    try:
        view = read_agent_status(path, max_age_s=max_age_s)
    except FileNotFoundError:
        print(f"agent status unavailable: {path} does not exist", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"agent status invalid: {exc}", file=sys.stderr)
        return 2

    age = "-" if view.age_s is None else f"{view.age_s:.1f}s"
    runtime = view.runtime
    health = "healthy" if not view.stale else "stale"
    print(
        f"state={view.state} health={health} pid={view.pid or '-'} age={age} "
        f"ticks={runtime.get('ticks', '-')} "
        f"host_samples={runtime.get('host_samples_persisted', '-')} "
        f"system_points={runtime.get('system_points_persisted', '-')}"
    )
    for key in (
        "last_host_storage_error",
        "last_system_storage_error",
        "last_retention_error",
    ):
        value = runtime.get(key)
        if value:
            print(f"{key}={value}")
    return 0 if view.state == "running" and not view.stale else 3


def _run_agent(args) -> int:
    log = logging.getLogger("profiler.agent")
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    database = Path(args.database).expanduser()
    status_path = Path(args.status_file).expanduser()
    started_at = time.time()
    pid = os.getpid()
    log.info("starting headless agent; database=%s", database)

    runtime: LocalAgentRuntime | None = None
    snapshot = None
    status_error: str | None = None
    try:
        with SQLiteTelemetryStore(database) as store:
            runtime = LocalAgentRuntime(store)
            runtime.start()
            snapshot = runtime.snapshot()
            try:
                write_agent_status(
                    status_path,
                    state="running",
                    pid=pid,
                    started_at=started_at,
                    runtime_snapshot=snapshot,
                    database_path=database,
                )
            except Exception as exc:
                status_error = str(exc)
                log.warning("could not write agent status: %s", exc)

            next_heartbeat = time.monotonic() + args.heartbeat_interval
            while not stop_event.wait(args.poll_interval):
                runtime.run_once()
                now = time.monotonic()
                if now >= next_heartbeat:
                    snapshot = runtime.snapshot()
                    try:
                        write_agent_status(
                            status_path,
                            state="running",
                            pid=pid,
                            started_at=started_at,
                            runtime_snapshot=snapshot,
                            database_path=database,
                        )
                        status_error = None
                    except Exception as exc:
                        status_error = str(exc)
                        log.warning("could not update agent heartbeat: %s", exc)
                    # Schedule from now: suspend or a blocked disk must not cause
                    # a burst of stale heartbeat writes after recovery.
                    next_heartbeat = now + args.heartbeat_interval

            runtime.stop()
            snapshot = runtime.snapshot()
            try:
                write_agent_status(
                    status_path,
                    state="stopped",
                    pid=pid,
                    started_at=started_at,
                    runtime_snapshot=snapshot,
                    database_path=database,
                )
                status_error = None
            except Exception as exc:
                status_error = str(exc)
                log.warning("could not write final agent status: %s", exc)
    except KeyboardInterrupt:
        stop_event.set()
        if runtime is not None:
            runtime.stop()
            snapshot = runtime.snapshot()
        return 130
    except Exception:
        log.exception("agent terminated unexpectedly")
        return 1

    if snapshot is not None:
        log.info(
            "agent stopped; ticks=%d host_samples=%d system_points=%d",
            snapshot.ticks,
            snapshot.host_samples_persisted,
            snapshot.system_points_persisted,
        )
    if status_error:
        log.warning("status file degraded at shutdown: %s", status_error)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    if args.heartbeat_interval <= 0:
        parser.error("--heartbeat-interval must be positive")
    if args.status_max_age <= 0:
        parser.error("--status-max-age must be positive")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.status:
        return _print_status(Path(args.status_file).expanduser(), max_age_s=args.status_max_age)
    return _run_agent(args)


if __name__ == "__main__":
    raise SystemExit(main())
