"""Headless Docker Autodiscovery owner for the OpenMetrics runtime."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import threading
import time
from typing import Any

from .docker_monitor import DockerMonitor
from .openmetrics import OpenMetricsCollector
from .openmetrics_runtime import OpenMetricsRuntimeWorker, resolve_openmetrics_for_containers


@dataclass(frozen=True, slots=True)
class AgentOpenMetricsSnapshot:
    running: bool
    refreshes: int
    refresh_failures: int
    containers_seen: int
    target_count: int
    discovery_errors: tuple[str, ...]
    last_docker_error: str | None
    last_sync_error: str | None
    last_refresh_at: float | None
    worker: object | None = None


class AgentOpenMetricsWorker:
    """Own Docker discovery and OpenMetrics scraping outside the GUI lifecycle.

    Discovery runs on its own daemon thread so an unavailable or slow Docker
    daemon cannot block Agent heartbeat/persistence work. A failed Docker scan
    preserves the last-known-good target set; an authoritative successful empty
    scan removes targets. The composed OpenMetrics worker supplies the bounded
    metric buffer consumed by :class:`LocalAgentRuntime`.
    """

    def __init__(
        self,
        *,
        monitor: DockerMonitor | None = None,
        monitor_factory: Callable[[], DockerMonitor] = DockerMonitor,
        metrics_worker: OpenMetricsRuntimeWorker | None = None,
        resolver: Callable[[Iterable[Any]], Any] = resolve_openmetrics_for_containers,
        discovery_interval_s: float = 10.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if discovery_interval_s <= 0:
            raise ValueError("discovery_interval_s must be positive")
        self._monitor = monitor
        self._monitor_factory = monitor_factory
        self.metrics_worker = metrics_worker or OpenMetricsRuntimeWorker(
            OpenMetricsCollector(max_payload_bytes=4 * 1024 * 1024, timeout_s=5.0),
            max_idle_wait_s=0.5,
        )
        self._resolver = resolver
        self.discovery_interval_s = float(discovery_interval_s)
        self._wall_clock = wall_clock
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._refreshes = 0
        self._refresh_failures = 0
        self._containers_seen = 0
        self._target_count = 0
        self._discovery_errors: tuple[str, ...] = ()
        self._last_docker_error: str | None = None
        self._last_sync_error: str | None = None
        self._last_refresh_at: float | None = None

    @property
    def monitor(self) -> DockerMonitor | None:
        return self._monitor

    def _ensure_monitor(self) -> DockerMonitor:
        monitor = self._monitor
        if monitor is None:
            monitor = self._monitor_factory()
            self._monitor = monitor
        return monitor

    def refresh_once(self) -> bool:
        """Perform one reconciliation; return True only after targets are synced."""
        try:
            monitor = self._ensure_monitor()
            containers = tuple(monitor.list_containers(all=True))
            docker_error = monitor.last_error
        except Exception as exc:
            with self._state_lock:
                self._refresh_failures += 1
                self._last_docker_error = str(exc)
                self._last_refresh_at = self._wall_clock()
            return False

        if docker_error:
            # Empty + error is not authoritative. Keep the previous target set
            # across Docker Desktop restarts/transient socket failures.
            with self._state_lock:
                self._refresh_failures += 1
                self._last_docker_error = str(docker_error)
                self._last_refresh_at = self._wall_clock()
            return False

        try:
            resolved = self._resolver(containers)
            targets = tuple(resolved.targets)
            errors = tuple(resolved.errors)
            if self._stop_event.is_set():
                return False
            self.metrics_worker.replace_targets(targets)
        except Exception as exc:
            with self._state_lock:
                self._refresh_failures += 1
                self._last_docker_error = None
                self._last_sync_error = str(exc)
                self._last_refresh_at = self._wall_clock()
            return False

        with self._state_lock:
            self._refreshes += 1
            self._containers_seen = len(containers)
            self._target_count = len(targets)
            self._discovery_errors = errors
            self._last_docker_error = None
            self._last_sync_error = None
            self._last_refresh_at = self._wall_clock()
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_once()
            if self._stop_event.wait(self.discovery_interval_s):
                break

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop_event.clear()
        self.metrics_worker.start()
        try:
            thread = threading.Thread(
                target=self._run,
                name="container-profiler-openmetrics-discovery",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except Exception:
            self.metrics_worker.stop(timeout_s=2.0)
            self._thread = None
            raise

    def stop(self, timeout_s: float = 2.0) -> bool:
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
        discovery_stopped = thread is None or not thread.is_alive()
        metrics_stopped = bool(self.metrics_worker.stop(timeout_s=timeout_s))
        return bool(discovery_stopped and metrics_stopped)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stop_event.is_set())

    def drain_points(self, limit: int = 1000):
        return self.metrics_worker.drain_points(limit)

    def requeue_points(self, points) -> int:
        return self.metrics_worker.requeue_points(points)

    def snapshot(self) -> AgentOpenMetricsSnapshot:
        with self._state_lock:
            values = (
                self._refreshes,
                self._refresh_failures,
                self._containers_seen,
                self._target_count,
                self._discovery_errors,
                self._last_docker_error,
                self._last_sync_error,
                self._last_refresh_at,
            )
        return AgentOpenMetricsSnapshot(
            running=self.is_running(),
            refreshes=values[0],
            refresh_failures=values[1],
            containers_seen=values[2],
            target_count=values[3],
            discovery_errors=values[4],
            last_docker_error=values[5],
            last_sync_error=values[6],
            last_refresh_at=values[7],
            worker=self.metrics_worker.snapshot(),
        )
