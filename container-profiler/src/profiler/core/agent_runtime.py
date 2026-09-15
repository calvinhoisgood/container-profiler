"""Headless local Agent runtime independent from Qt and Docker UI lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import threading
import time
from typing import Callable

from .agent_forwarding import AgentForwardingRuntime
from .agent_openmetrics import AgentOpenMetricsWorker
from .agent_self_metrics import normalize_agent_self_metrics
from .container_metrics import ContainerMetricsWorker
from .host_monitor import HostRuntimeWorker
from .statsd_metrics import StatsDMetricsWorker
from .storage import SQLiteTelemetryStore
from .system_metrics import SystemMetricsRuntimeWorker


@dataclass(frozen=True, slots=True)
class AgentRuntimeSnapshot:
    running: bool
    ticks: int
    host_samples_persisted: int
    system_points_persisted: int
    host_storage_failures: int
    system_storage_failures: int
    retention_failures: int
    last_host_storage_error: str | None
    last_system_storage_error: str | None
    last_retention_error: str | None
    statsd_points_persisted: int = 0
    statsd_storage_failures: int = 0
    last_statsd_storage_error: str | None = None
    statsd: object | None = None
    openmetrics_points_persisted: int = 0
    openmetrics_storage_failures: int = 0
    last_openmetrics_storage_error: str | None = None
    openmetrics: object | None = None
    container_points_persisted: int = 0
    container_storage_failures: int = 0
    last_container_storage_error: str | None = None
    containers: object | None = None
    self_points_persisted: int = 0
    self_storage_failures: int = 0
    last_self_storage_error: str | None = None
    forwarding: object | None = None


class LocalAgentRuntime:
    def __init__(
        self,
        store: SQLiteTelemetryStore,
        *,
        host_worker: HostRuntimeWorker | None = None,
        system_worker: SystemMetricsRuntimeWorker | None = None,
        statsd_worker: StatsDMetricsWorker | None = None,
        openmetrics_worker: AgentOpenMetricsWorker | None = None,
        container_worker: ContainerMetricsWorker | None = None,
        forwarding_worker: AgentForwardingRuntime | None = None,
        drain_limit: int = 1000,
        retention_s: float = 7 * 24 * 60 * 60,
        retention_max_rows: int = 250_000,
        retention_interval_s: float = 300.0,
        self_metrics_interval_s: float = 10.0,
        hostname: Callable[[], str] = socket.gethostname,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if drain_limit <= 0:
            raise ValueError("drain_limit must be positive")
        if (
            retention_s <= 0
            or retention_interval_s <= 0
            or retention_max_rows <= 0
            or self_metrics_interval_s <= 0
        ):
            raise ValueError("retention/self-metric settings must be positive")
        self.store = store
        self.host_worker = host_worker or HostRuntimeWorker(interval_s=1, max_queue=3600)
        self.system_worker = system_worker or SystemMetricsRuntimeWorker(
            interval_s=1,
            max_points=20_000,
            max_bytes=16 * 1024 * 1024,
        )
        self.statsd_worker = statsd_worker
        self.openmetrics_worker = openmetrics_worker
        self.container_worker = container_worker
        self.forwarding_worker = forwarding_worker
        self.drain_limit = drain_limit
        self.retention_s = retention_s
        self.retention_max_rows = retention_max_rows
        self.retention_interval_s = retention_interval_s
        self.self_metrics_interval_s = float(self_metrics_interval_s)
        self.hostname = hostname
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self._running = False
        self._ticks = 0
        self._host_persisted = 0
        self._system_persisted = 0
        self._statsd_persisted = 0
        self._openmetrics_persisted = 0
        self._container_persisted = 0
        self._self_persisted = 0
        self._host_storage_failures = 0
        self._system_storage_failures = 0
        self._statsd_storage_failures = 0
        self._openmetrics_storage_failures = 0
        self._container_storage_failures = 0
        self._self_storage_failures = 0
        self._retention_failures = 0
        self._last_host_storage_error = None
        self._last_system_storage_error = None
        self._last_statsd_storage_error = None
        self._last_openmetrics_storage_error = None
        self._last_container_storage_error = None
        self._last_self_storage_error = None
        self._last_retention_error = None
        self._next_retention = 0.0
        self._next_self_metrics = float("inf")

    def start(self):
        if self._running:
            return
        started = []
        try:
            if self.forwarding_worker:
                self.forwarding_worker.start()
                started.append((self.forwarding_worker, 6))
            self.host_worker.start()
            started.append((self.host_worker, 2))
            self.system_worker.start()
            started.append((self.system_worker, 2))
            if self.statsd_worker:
                self.statsd_worker.start()
                started.append((self.statsd_worker, 2))
            if self.openmetrics_worker:
                self.openmetrics_worker.start()
                started.append((self.openmetrics_worker, 2))
            if self.container_worker:
                self.container_worker.start()
                started.append((self.container_worker, 2))
        except Exception:
            for worker, timeout in reversed(started):
                try:
                    worker.stop(timeout_s=timeout)
                except Exception:
                    pass
            raise
        self._running = True
        now = self.monotonic_clock()
        self._next_retention = now + self.retention_interval_s
        self._next_self_metrics = now + self.self_metrics_interval_s

    def _drain_host(self):
        items = self.host_worker.drain(self.drain_limit)
        if not items:
            return
        try:
            self._host_persisted += self.store.append_host_samples(items)
            self._last_host_storage_error = None
        except Exception as exc:
            self.host_worker.requeue_front(items)
            self._host_storage_failures += 1
            self._last_host_storage_error = str(exc)

    def _metric_storage_failure(self, source: str, error: str) -> None:
        if source == "system":
            self._system_storage_failures += 1
            self._last_system_storage_error = error
        elif source == "statsd":
            self._statsd_storage_failures += 1
            self._last_statsd_storage_error = error
        elif source == "openmetrics":
            self._openmetrics_storage_failures += 1
            self._last_openmetrics_storage_error = error
        elif source == "container":
            self._container_storage_failures += 1
            self._last_container_storage_error = error

    def _metric_storage_success(self, source: str, written: int) -> None:
        if source == "system":
            self._system_persisted += written
            self._last_system_storage_error = None
        elif source == "statsd":
            self._statsd_persisted += written
            self._last_statsd_storage_error = None
        elif source == "openmetrics":
            self._openmetrics_persisted += written
            self._last_openmetrics_storage_error = None
        elif source == "container":
            self._container_persisted += written
            self._last_container_storage_error = None

    def _drain_metrics(self, worker, source: str):
        items = worker.drain_points(self.drain_limit)
        if not items:
            return
        try:
            written = self.store.append_custom_metrics(items)
        except Exception as exc:
            worker.requeue_points(items)
            self._metric_storage_failure(source, str(exc))
            return

        self._metric_storage_success(source, written)
        if self.forwarding_worker:
            self.forwarding_worker.enqueue(items)

    def _emit_self_metrics(self) -> None:
        now = self.monotonic_clock()
        if now < self._next_self_metrics:
            return
        try:
            points = normalize_agent_self_metrics(
                self.snapshot(),
                timestamp=self.wall_clock(),
                hostname=str(self.hostname() or "unknown"),
            )
            written = self.store.append_custom_metrics(points)
            self._self_persisted += written
            self._last_self_storage_error = None
            if self.forwarding_worker:
                self.forwarding_worker.enqueue(points)
        except Exception as exc:
            self._self_storage_failures += 1
            self._last_self_storage_error = str(exc)
        finally:
            self._next_self_metrics = now + self.self_metrics_interval_s

    def _retention(self):
        now = self.monotonic_clock()
        if now < self._next_retention:
            return
        try:
            self.store.prune_host_samples(
                before_timestamp=self.wall_clock() - self.retention_s,
                max_rows=self.retention_max_rows,
            )
            self.store.prune_custom_metrics(
                before_timestamp=self.wall_clock() - self.retention_s,
                max_rows=self.retention_max_rows,
            )
            self._last_retention_error = None
        except Exception as exc:
            self._retention_failures += 1
            self._last_retention_error = str(exc)
        finally:
            self._next_retention = now + self.retention_interval_s

    def run_once(self):
        if self.forwarding_worker:
            self.forwarding_worker.tick()
        self._drain_host()
        self._drain_metrics(self.system_worker, "system")
        if self.statsd_worker:
            self._drain_metrics(self.statsd_worker, "statsd")
        if self.openmetrics_worker:
            self._drain_metrics(self.openmetrics_worker, "openmetrics")
        if self.container_worker:
            self._drain_metrics(self.container_worker, "container")
        self._retention()
        self._ticks += 1
        self._emit_self_metrics()

    def run_forever(self, stop_event: threading.Event, *, poll_interval_s: float = 0.5):
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.start()
        try:
            while not stop_event.wait(poll_interval_s):
                self.run_once()
        finally:
            self.stop()

    def stop(self):
        if not self._running:
            return True
        results = [
            self.host_worker.stop(timeout_s=2),
            self.system_worker.stop(timeout_s=2),
        ]
        if self.statsd_worker:
            results.append(self.statsd_worker.stop(timeout_s=2))
        if self.openmetrics_worker:
            results.append(self.openmetrics_worker.stop(timeout_s=2))
        if self.container_worker:
            results.append(self.container_worker.stop(timeout_s=2))
        self.run_once()
        if self.forwarding_worker:
            results.append(self.forwarding_worker.stop(timeout_s=6))
        self._running = False
        return all(bool(value) for value in results)

    def snapshot(self):
        return AgentRuntimeSnapshot(
            running=self._running,
            ticks=self._ticks,
            host_samples_persisted=self._host_persisted,
            system_points_persisted=self._system_persisted,
            host_storage_failures=self._host_storage_failures,
            system_storage_failures=self._system_storage_failures,
            retention_failures=self._retention_failures,
            last_host_storage_error=self._last_host_storage_error,
            last_system_storage_error=self._last_system_storage_error,
            last_retention_error=self._last_retention_error,
            statsd_points_persisted=self._statsd_persisted,
            statsd_storage_failures=self._statsd_storage_failures,
            last_statsd_storage_error=self._last_statsd_storage_error,
            statsd=None if self.statsd_worker is None else self.statsd_worker.snapshot(),
            openmetrics_points_persisted=self._openmetrics_persisted,
            openmetrics_storage_failures=self._openmetrics_storage_failures,
            last_openmetrics_storage_error=self._last_openmetrics_storage_error,
            openmetrics=(
                None if self.openmetrics_worker is None else self.openmetrics_worker.snapshot()
            ),
            container_points_persisted=self._container_persisted,
            container_storage_failures=self._container_storage_failures,
            last_container_storage_error=self._last_container_storage_error,
            containers=(
                None if self.container_worker is None else self.container_worker.snapshot()
            ),
            self_points_persisted=self._self_persisted,
            self_storage_failures=self._self_storage_failures,
            last_self_storage_error=self._last_self_storage_error,
            forwarding=(
                None if self.forwarding_worker is None else self.forwarding_worker.snapshot()
            ),
        )


class LocalAgentProcess:
    def __init__(self, database_path: str | Path, **runtime_kwargs):
        self.database_path = Path(database_path)
        self.runtime_kwargs = runtime_kwargs

    def run(self, stop_event: threading.Event):
        with SQLiteTelemetryStore(self.database_path) as store:
            LocalAgentRuntime(store, **self.runtime_kwargs).run_forever(stop_event)
