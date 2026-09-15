"""Normalize native host I/O into Datadog-compatible system metric names.

The native collectors own platform-specific counter acquisition. This module
owns metric semantics, tags, buffering and scheduling so Windows and Linux emit
the same contract. Rate metrics are deliberately omitted on first observation,
a counter reset, or an excessive sampling gap rather than inventing a zero.
"""
from __future__ import annotations

from dataclasses import dataclass
import socket
import threading
import time
from typing import Callable, Iterable

from .custom_metrics import BoundedMetricBuffer, CustomMetricPoint, MetricBufferStats
from .host_io import (
    HostIORateTracker,
    HostIOSnapshot,
    IORate,
    create_native_io_backend,
)


@dataclass(frozen=True, slots=True)
class HostIOCollection:
    snapshot: HostIOSnapshot
    network_rates: tuple[IORate, ...]
    disk_rates: tuple[IORate, ...]


@dataclass(frozen=True, slots=True)
class SystemMetricsWorkerSnapshot:
    running: bool
    collections: int
    failed_collections: int
    partial_collections: int
    last_error: str | None
    network_error: str | None
    disk_error: str | None
    buffer: MetricBufferStats


def _tags(hostname: str, device: str) -> tuple[str, str]:
    return (f"host:{hostname}", f"device:{device}")


def normalize_host_io_metrics(
    collection: HostIOCollection,
    *,
    hostname: str,
) -> tuple[CustomMetricPoint, ...]:
    """Convert raw host I/O plus derived rates into the shared metric model.

    Names intentionally follow Datadog's documented system/network check
    namespaces so the local Explorer and optional forwarder can use the same
    vocabulary. Raw packet/error/drop counters are gauges. Throughput and IOPS
    are already interval-derived gauges.
    """
    hostname = hostname.strip() or "unknown"
    timestamp = float(collection.snapshot.timestamp)
    network_rates = {rate.name: rate for rate in collection.network_rates}
    disk_rates = {rate.name: rate for rate in collection.disk_rates}
    points: list[CustomMetricPoint] = []

    for counters in collection.snapshot.network:
        tags = _tags(hostname, counters.interface)
        for name, value, unit in (
            ("system.net.packets_in.count", counters.rx_packets, "packet"),
            ("system.net.packets_out.count", counters.tx_packets, "packet"),
            ("system.net.packets_in.error", counters.rx_errors, "error"),
            ("system.net.packets_out.error", counters.tx_errors, "error"),
            ("system.net.packets_in.drop", counters.rx_dropped, "packet"),
            ("system.net.packets_out.drop", counters.tx_dropped, "packet"),
        ):
            points.append(CustomMetricPoint(
                timestamp=timestamp,
                name=name,
                value=float(value),
                tags=tags,
                metric_type="gauge",
                unit=unit,
                source="system",
                target_key="host-network",
            ))

        rate = network_rates.get(counters.interface)
        if rate is not None and rate.read_bps is not None:
            points.append(CustomMetricPoint(
                timestamp=timestamp,
                name="system.net.bytes_rcvd",
                value=float(rate.read_bps),
                tags=tags,
                metric_type="gauge",
                unit="byte",
                source="system",
                target_key="host-network",
            ))
        if rate is not None and rate.write_bps is not None:
            points.append(CustomMetricPoint(
                timestamp=timestamp,
                name="system.net.bytes_sent",
                value=float(rate.write_bps),
                tags=tags,
                metric_type="gauge",
                unit="byte",
                source="system",
                target_key="host-network",
            ))

    for counters in collection.snapshot.disks:
        rate = disk_rates.get(counters.device)
        if rate is None:
            continue
        tags = _tags(hostname, counters.device)
        for name, value, unit in (
            ("system.io.r_s", rate.read_ops_s, "request"),
            ("system.io.w_s", rate.write_ops_s, "request"),
            (
                "system.io.rkb_s",
                None if rate.read_bps is None else rate.read_bps / 1024.0,
                "kibibyte",
            ),
            (
                "system.io.wkb_s",
                None if rate.write_bps is None else rate.write_bps / 1024.0,
                "kibibyte",
            ),
        ):
            if value is None:
                continue
            points.append(CustomMetricPoint(
                timestamp=timestamp,
                name=name,
                value=float(value),
                tags=tags,
                metric_type="gauge",
                unit=unit,
                source="system",
                target_key="host-io",
            ))

    return tuple(points)


class NativeSystemMetricsCollector:
    """Platform-neutral facade over native I/O counters and rate derivation."""

    def __init__(
        self,
        backend=None,
        *,
        tracker: HostIORateTracker | None = None,
        hostname: Callable[[], str] = socket.gethostname,
    ) -> None:
        self.backend = backend or create_native_io_backend()
        self.tracker = tracker or HostIORateTracker()
        self.hostname = hostname
        self.last_network_error: str | None = None
        self.last_disk_error: str | None = None

    def collect(self) -> tuple[CustomMetricPoint, ...]:
        snapshot = self.backend.read()
        self.last_network_error = snapshot.network_error
        self.last_disk_error = snapshot.disk_error
        collection = HostIOCollection(
            snapshot=snapshot,
            network_rates=self.tracker.update_network(snapshot.timestamp, snapshot.network),
            disk_rates=self.tracker.update_disks(snapshot.timestamp, snapshot.disks),
        )
        return normalize_host_io_metrics(
            collection,
            hostname=str(self.hostname() or "unknown"),
        )


class SystemMetricsRuntimeWorker:
    """Continuously collect system I/O into a bounded shared metric buffer."""

    def __init__(
        self,
        collector: NativeSystemMetricsCollector | None = None,
        *,
        interval_s: float = 1.0,
        max_points: int = 20_000,
        max_bytes: int = 16 * 1024 * 1024,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.collector = collector or NativeSystemMetricsCollector()
        self.interval_s = float(interval_s)
        self.monotonic_clock = monotonic_clock
        self.buffer = BoundedMetricBuffer(max_points=max_points, max_bytes=max_bytes)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._collections = 0
        self._failed_collections = 0
        self._partial_collections = 0
        self._last_error: str | None = None

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.is_running():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run,
                name="native-system-metrics",
                daemon=True,
            )
            self._thread.start()

    def collect_once(self) -> int:
        """Run one collection cycle; exposed for deterministic unit tests."""
        try:
            points = self.collector.collect()
        except Exception as exc:
            with self._condition:
                self._failed_collections += 1
                self._last_error = str(exc)
            return 0

        accepted = self.buffer.append_many(points)
        with self._condition:
            self._collections += 1
            self._last_error = None
            if self.collector.last_network_error or self.collector.last_disk_error:
                self._partial_collections += 1
        return accepted

    def drain_points(self, limit: int = 1000) -> tuple[CustomMetricPoint, ...]:
        return self.buffer.drain(limit)

    def requeue_points(self, points: Iterable[CustomMetricPoint]) -> int:
        return self.buffer.requeue_front(points)

    def snapshot(self) -> SystemMetricsWorkerSnapshot:
        with self._condition:
            return SystemMetricsWorkerSnapshot(
                running=self.is_running(),
                collections=self._collections,
                failed_collections=self._failed_collections,
                partial_collections=self._partial_collections,
                last_error=self._last_error,
                network_error=self.collector.last_network_error,
                disk_error=self.collector.last_disk_error,
                buffer=self.buffer.stats(),
            )

    def stop(self, timeout_s: float = 2.0) -> bool:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout_s))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _run(self) -> None:
        next_due = float(self.monotonic_clock())
        while True:
            with self._condition:
                if self._stop_requested:
                    return
            now = float(self.monotonic_clock())
            if now < next_due:
                with self._condition:
                    if self._stop_requested:
                        return
                    self._condition.wait(min(next_due - now, 0.25))
                continue
            self.collect_once()
            finished = float(self.monotonic_clock())
            # Slow native calls and suspend never trigger a catch-up burst.
            next_due = max(next_due + self.interval_s, finished + self.interval_s)
