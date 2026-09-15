"""Headless Container Profiler Agent entry point; imports no Qt modules."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from profiler.core.agent_forwarding import AgentForwardingRuntime
from profiler.core.agent_openmetrics import AgentOpenMetricsWorker
from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.agent_status import read_agent_status, write_agent_status
from profiler.core.container_metrics import ContainerMetricsWorker
from profiler.core.metric_alerts import MetricAlertRuntime
from profiler.core.statsd_metrics import StatsDMetricsWorker
from profiler.core.storage import SQLiteTelemetryStore
from profiler.utils.paths import (
    get_agent_status_path,
    get_forwarding_config_path,
    get_forwarding_db_path,
    get_telemetry_db_path,
)


def build_parser():
    p = argparse.ArgumentParser(description="Container Profiler headless agent")
    p.add_argument("--database", type=Path, default=get_telemetry_db_path())
    p.add_argument("--poll-interval", type=float, default=0.5)
    p.add_argument("--status-file", type=Path, default=get_agent_status_path())
    p.add_argument("--heartbeat-interval", type=float, default=5.0)
    p.add_argument("--status", action="store_true")
    p.add_argument("--status-max-age", type=float, default=15.0)
    p.add_argument("--self-metrics-interval", type=float, default=10.0)
    p.add_argument("--container-interval", type=float, default=2.0)
    p.add_argument("--container-max", type=int, default=256)
    p.add_argument(
        "--no-container-metrics",
        action="store_true",
        help="Disable headless all-container Docker resource collection",
    )
    p.add_argument(
        "--dogstatsd-host",
        default="127.0.0.1",
        help="DogStatsD bind host; loopback by default",
    )
    p.add_argument("--dogstatsd-port", type=int, default=8125)
    p.add_argument("--dogstatsd-flush-interval", type=float, default=10.0)
    p.add_argument(
        "--no-dogstatsd",
        action="store_true",
        help="Disable local DogStatsD UDP ingestion",
    )
    p.add_argument(
        "--openmetrics-discovery-interval",
        type=float,
        default=10.0,
        help="Seconds between Docker OpenMetrics Autodiscovery reconciliation scans",
    )
    p.add_argument(
        "--no-openmetrics",
        action="store_true",
        help="Disable Docker label based OpenMetrics/Prometheus Autodiscovery",
    )
    p.add_argument(
        "--metric-alerts-config",
        type=Path,
        default=None,
        help="Optional hot-reloadable JSON threshold rules for persisted custom metrics",
    )
    p.add_argument(
        "--metric-alerts-max-series",
        type=int,
        default=5000,
        help="Bound independent alert state cardinality across target/tag series",
    )
    p.add_argument(
        "--forwarding-config",
        type=Path,
        default=get_forwarding_config_path(),
        help="Disabled-by-default remote forwarding JSON configuration",
    )
    p.add_argument(
        "--forwarding-spool",
        type=Path,
        default=get_forwarding_db_path(),
        help="Durable bounded outbound forwarding spool",
    )
    p.add_argument("--forwarding-reload-interval", type=float, default=2.0)
    p.add_argument(
        "--no-forwarding",
        action="store_true",
        help="Disable forwarding runtime even if its configuration enables it",
    )
    p.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return p


def _install_signal_handlers(stop_event):
    def request_stop(signum, frame):
        del frame
        logging.getLogger("profiler.agent").info(
            "shutdown requested by signal %s", signum
        )
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, request_stop)
            except (OSError, ValueError):
                pass


def _print_status(path, max_age_s):
    try:
        view = read_agent_status(path, max_age_s=max_age_s)
    except FileNotFoundError:
        print(f"agent status unavailable: {path} does not exist", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"agent status invalid: {exc}", file=sys.stderr)
        return 2
    r = view.runtime
    age = "-" if view.age_s is None else f"{view.age_s:.1f}s"
    health = "healthy" if not view.stale else "stale"
    print(
        f"state={view.state} health={health} pid={view.pid or '-'} age={age} "
        f"ticks={r.get('ticks', '-')} host_samples={r.get('host_samples_persisted', '-')} "
        f"system_points={r.get('system_points_persisted', '-')} "
        f"container_points={r.get('container_points_persisted', '-')} "
        f"dogstatsd_points={r.get('statsd_points_persisted', '-')} "
        f"openmetrics_points={r.get('openmetrics_points_persisted', '-')} "
        f"alerts={r.get('alert_events_persisted', '-')} "
        f"self_points={r.get('self_points_persisted', '-')}"
    )
    for key in (
        "last_host_storage_error",
        "last_system_storage_error",
        "last_container_storage_error",
        "last_statsd_storage_error",
        "last_openmetrics_storage_error",
        "last_alert_error",
        "last_self_storage_error",
        "last_retention_error",
    ):
        if r.get(key):
            print(f"{key}={r[key]}")
    containers = r.get("containers")
    if isinstance(containers, dict):
        if containers.get("last_docker_error"):
            print(f"container_docker_error={containers['last_docker_error']}")
        if containers.get("last_sample_errors"):
            print("container_sample_errors=" + "; ".join(containers["last_sample_errors"]))
    openmetrics = r.get("openmetrics")
    if isinstance(openmetrics, dict):
        if openmetrics.get("last_docker_error"):
            print(f"openmetrics_docker_error={openmetrics['last_docker_error']}")
        if openmetrics.get("last_sync_error"):
            print(f"openmetrics_sync_error={openmetrics['last_sync_error']}")
    metric_alerts = r.get("metric_alerts")
    if isinstance(metric_alerts, dict):
        print(
            f"metric_alerts=rules:{metric_alerts.get('rule_count', 0)} "
            f"series:{metric_alerts.get('tracked_series', 0)} "
            f"evicted:{metric_alerts.get('evicted_series', 0)}"
        )
        if metric_alerts.get("last_config_error"):
            print(f"metric_alerts_config_error={metric_alerts['last_config_error']}")
    forwarding = r.get("forwarding")
    if isinstance(forwarding, dict):
        queue = forwarding.get("queue") or {}
        print(
            "forwarding="
            + ("enabled" if forwarding.get("enabled") else "disabled")
            + f" queued={queue.get('queued_items', 0)} dropped={queue.get('dropped_items', 0)}"
        )
        for key in ("last_config_error", "last_enqueue_error", "last_runtime_error"):
            if forwarding.get(key):
                print(f"forwarding_{key}={forwarding[key]}")
    return 0 if view.state == "running" and not view.stale else 3


def _runtime_for(store, args):
    containers = None
    if not args.no_container_metrics:
        containers = ContainerMetricsWorker(
            interval_s=args.container_interval,
            max_containers=args.container_max,
        )
    statsd = None
    if not args.no_dogstatsd:
        statsd = StatsDMetricsWorker(
            host=args.dogstatsd_host,
            port=args.dogstatsd_port,
            flush_interval_s=args.dogstatsd_flush_interval,
        )
    openmetrics = None
    if not args.no_openmetrics:
        openmetrics = AgentOpenMetricsWorker(
            discovery_interval_s=args.openmetrics_discovery_interval
        )
    metric_alerts = None
    if args.metric_alerts_config is not None:
        metric_alerts = MetricAlertRuntime(
            Path(args.metric_alerts_config).expanduser(),
            max_series=args.metric_alerts_max_series,
        )
    forwarding = None
    if not args.no_forwarding:
        forwarding = AgentForwardingRuntime(
            Path(args.forwarding_config).expanduser(),
            Path(args.forwarding_spool).expanduser(),
            reload_interval_s=args.forwarding_reload_interval,
        )
    return LocalAgentRuntime(
        store,
        statsd_worker=statsd,
        openmetrics_worker=openmetrics,
        container_worker=containers,
        forwarding_worker=forwarding,
        metric_alert_runtime=metric_alerts,
        self_metrics_interval_s=args.self_metrics_interval,
    )


def _run_agent(args):
    log = logging.getLogger("profiler.agent")
    stop = threading.Event()
    _install_signal_handlers(stop)
    database = Path(args.database).expanduser()
    status_path = Path(args.status_file).expanduser()
    started = time.time()
    pid = os.getpid()
    runtime = None
    snapshot = None
    log.info(
        "starting headless agent; database=%s containers=%s dogstatsd=%s openmetrics=%s alerts=%s forwarding=%s",
        database,
        "disabled" if args.no_container_metrics else f"{args.container_interval:g}s",
        "disabled"
        if args.no_dogstatsd
        else f"{args.dogstatsd_host}:{args.dogstatsd_port}",
        "disabled"
        if args.no_openmetrics
        else f"docker-autodiscovery/{args.openmetrics_discovery_interval:g}s",
        "disabled" if args.metric_alerts_config is None else str(args.metric_alerts_config),
        "disabled" if args.no_forwarding else str(args.forwarding_config),
    )
    try:
        with SQLiteTelemetryStore(database) as store:
            runtime = _runtime_for(store, args)
            runtime.start()
            snapshot = runtime.snapshot()
            try:
                write_agent_status(
                    status_path,
                    state="running",
                    pid=pid,
                    started_at=started,
                    runtime_snapshot=snapshot,
                    database_path=database,
                )
            except Exception as exc:
                log.warning("could not write agent status: %s", exc)
            next_heartbeat = time.monotonic() + args.heartbeat_interval
            while not stop.wait(args.poll_interval):
                runtime.run_once()
                now = time.monotonic()
                if now >= next_heartbeat:
                    snapshot = runtime.snapshot()
                    try:
                        write_agent_status(
                            status_path,
                            state="running",
                            pid=pid,
                            started_at=started,
                            runtime_snapshot=snapshot,
                            database_path=database,
                        )
                    except Exception as exc:
                        log.warning("could not update agent heartbeat: %s", exc)
                    next_heartbeat = now + args.heartbeat_interval
            runtime.stop()
            snapshot = runtime.snapshot()
            try:
                write_agent_status(
                    status_path,
                    state="stopped",
                    pid=pid,
                    started_at=started,
                    runtime_snapshot=snapshot,
                    database_path=database,
                )
            except Exception as exc:
                log.warning("could not write final agent status: %s", exc)
    except KeyboardInterrupt:
        stop.set()
        if runtime is not None:
            runtime.stop()
        return 130
    except Exception:
        log.exception("agent terminated unexpectedly")
        return 1
    if snapshot is not None:
        log.info(
            "agent stopped; ticks=%d host_samples=%d system_points=%d "
            "container_points=%d dogstatsd_points=%d openmetrics_points=%d "
            "alerts=%d self_points=%d",
            snapshot.ticks,
            snapshot.host_samples_persisted,
            snapshot.system_points_persisted,
            snapshot.container_points_persisted,
            snapshot.statsd_points_persisted,
            snapshot.openmetrics_points_persisted,
            snapshot.alert_events_persisted,
            snapshot.self_points_persisted,
        )
    return 0


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if (
        args.poll_interval <= 0
        or args.heartbeat_interval <= 0
        or args.status_max_age <= 0
        or args.self_metrics_interval <= 0
        or args.container_interval <= 0
        or args.dogstatsd_flush_interval <= 0
        or args.openmetrics_discovery_interval <= 0
        or args.forwarding_reload_interval <= 0
    ):
        p.error("intervals must be positive")
    if args.container_max <= 0:
        p.error("--container-max must be positive")
    if args.metric_alerts_max_series <= 0:
        p.error("--metric-alerts-max-series must be positive")
    if not 0 <= args.dogstatsd_port <= 65535:
        p.error("--dogstatsd-port must be in [0, 65535]")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.status:
        return _print_status(Path(args.status_file).expanduser(), args.status_max_age)
    return _run_agent(args)


if __name__ == "__main__":
    raise SystemExit(main())