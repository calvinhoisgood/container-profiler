"""Headless Container Profiler Agent entry point.

This process intentionally imports no Qt modules. It is suitable for foreground
operation today and is the runtime that future Windows Service/systemd wrappers
will host instead of keeping collection tied to the desktop Explorer.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
import sys
import threading

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.storage import SQLiteTelemetryStore
from profiler.utils.paths import get_telemetry_db_path


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_interval <= 0:
        build_parser().error("--poll-interval must be positive")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("profiler.agent")
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    database = Path(args.database).expanduser()
    log.info("starting headless agent; database=%s", database)
    try:
        with SQLiteTelemetryStore(database) as store:
            runtime = LocalAgentRuntime(store)
            runtime.run_forever(stop_event, poll_interval_s=args.poll_interval)
            snapshot = runtime.snapshot()
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("agent terminated unexpectedly")
        return 1

    log.info(
        "agent stopped; ticks=%d host_samples=%d system_points=%d",
        snapshot.ticks,
        snapshot.host_samples_persisted,
        snapshot.system_points_persisted,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
