"""Headless local Agent runtime independent from Qt and Docker UI lifecycle.

This is the service-oriented orchestration boundary: native host collectors feed
bounded queues, the foreground runtime drains them into durable SQLite storage,
and retention runs on its own monotonic deadline. The desktop can remain an
Explorer while future Windows Service/systemd entry points reuse this core.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Callable

from .host_monitor import HostRuntimeWorker
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


class LocalAgentRuntime:
    """Persist native host telemetry without requiring a GUI event loop."""

    def __init__(
        self,
        store: SQLiteTelemetryStore,
        *,
        host_worker: HostRuntimeWorker | None = None,
        system_worker: SystemMetricsRuntimeWorker | None = None,
        drain_limit: int = 1000,
        retention_s: float = 7 * 24 * 60 * 60,
        retention_max_rows: int = 250_000,
        retention_interval_s: float = 300.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if drain_limit <= 0:
            raise ValueError("drain_limit must be positive")
        if retention_s <= 0 or retention_interval_s <= 0 or retention_max_rows <= 0:
            raise ValueError("retention settings must be positive")
        self.store = store
        self.host_worker = host_worker or HostRuntimeWorker(interval_s=1.0, max_queue=3600)
        self.system_worker = system_worker or SystemMetricsRuntimeWorker(
            interval_s=1.0,
            max_points=20_000,
            max_bytes=16 * 1024 * 1024,
        )
        self.drain_limit = int(drain_limit)
        self.retention_s = float(retention_s)
        self.retention_max_rows = int(retention_max_rows)
        self.retention_interval_s = float(retention_interval_s)
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self._running = False
        self._ticks = 0
        self._host_persisted = 0
        self._system_persisted = 0
        self._host_storage_failures = 0
        self._system_storage_failures = 0
        self._retention_failures = 0
        self._last_host_storage_error: str | None = None
        self._last_system_storage_error: str | None = None
        self._last_retention_error: str | None = None
        self._next_retention = 0.0

    def start(self) -> None:
        if self._running:
            return
        self.host_worker.start()
        try:
            self.system_worker.start()
        except Exception:
            self.host_worker.stop(timeout_s=2.0)
            raise
        self._running = True
        self._next_retention = float(self.monotonic_clock()) + self.retention_interval_s

    def _drain_host(self) -> None:
        samples = self.host_worker.drain(self.drain_limit)
        if not samples:
            return
        try:
            written = self.store.append_host_samples(samples)
        except Exception as exc:
            self.host_worker.requeue_front(samples)
            self._host_storage_failures += 1
            self._last_host_storage_error = str(exc)
            return
        self._host_persisted += int(written)
        self._last_host_storage_error = None

    def _drain_system(self) -> None:
        points = self.system_worker.drain_points(self.drain_limit)
        if not points:
            return
        try:
            written = self.store.append_custom_metrics(points)
        except Exception as exc:
            self.system_worker.requeue_points(points)
            self._system_storage_failures += 1
            self._last_system_storage_error = str(exc)
            return
        self._system_persisted += int(written)
        self._last_system_storage_error = None

    def _run_retention_if_due(self) -> None:
        now = float(self.monotonic_clock())
        if now < self._next_retention:
            return
        cutoff = float(self.wall_clock()) - self.retention_s
        try:
            self.store.prune_host_samples(
                before_timestamp=cutoff,
                max_rows=self.retention_max_rows,
            )
            self.store.prune_custom_metrics(
                before_timestamp=cutoff,
                max_rows=self.retention_max_rows,
            )
            self._last_retention_error = None
        except Exception as exc:
            self._retention_failures += 1
            self._last_retention_error = str(exc)
        finally:
            # Never run a catch-up retention storm after suspend or DB stalls.
            self._next_retention = now + self.retention_interval_s

    def run_once(self) -> None:
        """Drain all current producer queues once; deterministic and testable."""
        self._drain_host()
        self._drain_system()
        self._run_retention_if_due()
        self._ticks += 1

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_interval_s: float = 0.5,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.start()
        try:
            while not stop_event.wait(poll_interval_s):
                self.run_once()
        finally:
            self.stop()

    def stop(self) -> bool:
        if not self._running:
            return True
        host_stopped = self.host_worker.stop(timeout_s=2.0)
        system_stopped = self.system_worker.stop(timeout_s=2.0)
        # Producers are stopped first, then a final drain gives queued telemetry
        # a chance to become durable before process/service shutdown.
        self.run_once()
        self._running = False
        return bool(host_stopped and system_stopped)

    def snapshot(self) -> AgentRuntimeSnapshot:
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
        )


class LocalAgentProcess:
    """Own the SQLite connection for a foreground/service process."""

    def __init__(self, database_path: str | Path, **runtime_kwargs) -> None:
        self.database_path = Path(database_path)
        self.runtime_kwargs = runtime_kwargs

    def run(self, stop_event: threading.Event) -> None:
        # SQLite is opened and used in this process/thread, avoiding accidental
        # cross-thread connection sharing with the desktop Explorer.
        with SQLiteTelemetryStore(self.database_path) as store:
            runtime = LocalAgentRuntime(store, **self.runtime_kwargs)
            runtime.run_forever(stop_event)
