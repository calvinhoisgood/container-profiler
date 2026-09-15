"""Headless all-container resource collection using Docker's native API."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import threading
import time
from typing import Any

from .custom_metrics import BoundedMetricBuffer, CustomMetricPoint, MetricBufferStats
from .docker_monitor import DockerMonitor
from .models import ContainerInfo, ContainerStats


@dataclass(frozen=True, slots=True)
class ContainerMetricsSnapshot:
    running: bool
    ticks: int
    discovery_failures: int
    sample_failures: int
    containers_seen: int
    containers_sampled: int
    containers_skipped: int
    last_docker_error: str | None
    last_sample_errors: tuple[str, ...]
    buffer: MetricBufferStats


def normalize_container_stats(
    container: ContainerInfo,
    stats: ContainerStats,
) -> tuple[CustomMetricPoint, ...]:
    """Map one Docker stats response into the shared metric model."""
    tags = tuple(dict.fromkeys(tuple(container.tags)))
    common = dict(
        timestamp=float(stats.timestamp),
        tags=tags,
        metric_type="gauge",
        source="docker",
        target_key=f"docker:{container.id}",
        container_id=container.id,
    )
    points = [
        CustomMetricPoint(
            name="container.cpu.usage_percent",
            value=float(stats.cpu_percent),
            unit="percent",
            **common,
        ),
        CustomMetricPoint(
            name="container.memory.usage_mib",
            value=float(stats.memory_mb),
            unit="MiB",
            **common,
        ),
        CustomMetricPoint(
            name="container.memory.limit_mib",
            value=float(stats.memory_limit_mb),
            unit="MiB",
            **common,
        ),
        CustomMetricPoint(
            name="container.memory.usage_percent",
            value=float(stats.memory_percent),
            unit="percent",
            **common,
        ),
        CustomMetricPoint(
            name="container.network.rx_bytes",
            value=float(stats.network_rx_bytes),
            unit="byte",
            **common,
        ),
        CustomMetricPoint(
            name="container.network.tx_bytes",
            value=float(stats.network_tx_bytes),
            unit="byte",
            **common,
        ),
    ]
    if stats.network_rx_bps is not None:
        points.append(
            CustomMetricPoint(
                name="container.network.rx_bytes_per_second",
                value=float(stats.network_rx_bps),
                unit="byte/s",
                **common,
            )
        )
    if stats.network_tx_bps is not None:
        points.append(
            CustomMetricPoint(
                name="container.network.tx_bytes_per_second",
                value=float(stats.network_tx_bps),
                unit="byte/s",
                **common,
            )
        )
    if stats.pids is not None:
        points.append(
            CustomMetricPoint(
                name="container.pids",
                value=float(stats.pids),
                unit="process",
                **common,
            )
        )
    return tuple(points)


class ContainerMetricsWorker:
    """Periodically sample every running Docker container off the Agent loop.

    Docker operations execute on a dedicated daemon thread. Discovery and
    individual container failures are isolated and surfaced in a snapshot;
    successfully collected containers in the same tick are still retained.
    """

    def __init__(
        self,
        *,
        monitor: DockerMonitor | None = None,
        monitor_factory: Callable[[], DockerMonitor] = DockerMonitor,
        interval_s: float = 2.0,
        max_containers: int = 256,
        max_error_details: int = 32,
        max_points: int = 50_000,
        max_bytes: int = 32 * 1024 * 1024,
        buffer: BoundedMetricBuffer | None = None,
    ) -> None:
        if interval_s <= 0 or max_containers <= 0 or max_error_details <= 0:
            raise ValueError("container collection limits must be positive")
        self._monitor = monitor
        self._monitor_factory = monitor_factory
        self.interval_s = float(interval_s)
        self.max_containers = int(max_containers)
        self.max_error_details = int(max_error_details)
        self.buffer = buffer or BoundedMetricBuffer(
            max_points=max_points,
            max_bytes=max_bytes,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._ticks = 0
        self._discovery_failures = 0
        self._sample_failures = 0
        self._containers_seen = 0
        self._containers_sampled = 0
        self._containers_skipped = 0
        self._last_docker_error: str | None = None
        self._last_sample_errors: tuple[str, ...] = ()

    @property
    def monitor(self) -> DockerMonitor | None:
        return self._monitor

    def _ensure_monitor(self) -> DockerMonitor:
        monitor = self._monitor
        if monitor is None:
            monitor = self._monitor_factory()
            self._monitor = monitor
        return monitor

    def collect_once(self) -> bool:
        try:
            monitor = self._ensure_monitor()
            containers = tuple(monitor.list_containers(all=False))
            discovery_error = monitor.last_error
        except Exception as exc:
            with self._state_lock:
                self._ticks += 1
                self._discovery_failures += 1
                self._last_docker_error = str(exc)
                self._last_sample_errors = ()
            return False

        if discovery_error:
            with self._state_lock:
                self._ticks += 1
                self._discovery_failures += 1
                self._last_docker_error = str(discovery_error)
                self._last_sample_errors = ()
            return False

        running = tuple(c for c in containers if str(c.status).lower() == "running")
        selected = running[: self.max_containers]
        skipped = max(0, len(running) - len(selected))
        errors: list[str] = []
        sampled = 0
        for container in selected:
            if self._stop_event.is_set():
                break
            try:
                stats = monitor.get_stats(container.id)
            except Exception as exc:
                stats = None
                detail = str(exc)
            else:
                detail = monitor.last_error or "Docker stats unavailable"
            if stats is None:
                if len(errors) < self.max_error_details:
                    errors.append(f"{container.id[:12]}: {detail}")
                continue
            sampled += 1
            self.buffer.append_many(normalize_container_stats(container, stats))

        failures = len(selected) - sampled
        with self._state_lock:
            self._ticks += 1
            self._sample_failures += failures
            self._containers_seen = len(running)
            self._containers_sampled += sampled
            self._containers_skipped += skipped
            self._last_docker_error = None
            self._last_sample_errors = tuple(errors)
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.collect_once()
            if self._stop_event.wait(self.interval_s):
                break

    def start(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run,
            name="container-profiler-docker-metrics",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout_s: float = 2.0) -> bool:
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
        return thread is None or not thread.is_alive()

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stop_event.is_set())

    def drain_points(self, limit: int = 1000) -> tuple[CustomMetricPoint, ...]:
        return self.buffer.drain(limit)

    def requeue_points(self, points: Iterable[CustomMetricPoint]) -> int:
        return self.buffer.requeue_front(points)

    def snapshot(self) -> ContainerMetricsSnapshot:
        with self._state_lock:
            values = (
                self._ticks,
                self._discovery_failures,
                self._sample_failures,
                self._containers_seen,
                self._containers_sampled,
                self._containers_skipped,
                self._last_docker_error,
                self._last_sample_errors,
            )
        return ContainerMetricsSnapshot(
            running=self.is_running(),
            ticks=values[0],
            discovery_failures=values[1],
            sample_failures=values[2],
            containers_seen=values[3],
            containers_sampled=values[4],
            containers_skipped=values[5],
            last_docker_error=values[6],
            last_sample_errors=values[7],
            buffer=self.buffer.stats(),
        )
