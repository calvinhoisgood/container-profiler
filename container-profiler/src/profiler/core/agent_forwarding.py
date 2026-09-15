"""Headless ownership of durable remote metric forwarding and hot reload."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from .custom_metrics import CustomMetricPoint
from .forwarding import DurableDeliveryQueue, DeliveryWorker, HTTPDeliveryTransport
from .forwarding_config import ForwardingConfig, load_forwarding_config
from .metric_forwarding import MetricEnqueueStats, enqueue_metric_points


@dataclass(frozen=True, slots=True)
class AgentForwardingSnapshot:
    running: bool
    enabled: bool
    reloads: int
    reload_failures: int
    enqueue_failures: int
    points_enqueued: int
    points_dropped: int
    last_config_error: str | None
    last_enqueue_error: str | None
    last_runtime_error: str | None
    config_path: str
    spool_path: str
    queue: object | None = None
    worker: object | None = None


class AgentForwardingRuntime:
    """Manage forwarding config, durable spool and delivery worker for the Agent.

    The configuration is disabled by default. Changes are reconciled by the
    Agent loop using file metadata, so no extra watcher dependency is required.
    Invalid replacements have last-known-good semantics: the currently working
    queue/transport stays active until a valid configuration is available.
    """

    def __init__(
        self,
        config_path: str | Path,
        spool_path: str | Path,
        *,
        reload_interval_s: float = 2.0,
        config_loader: Callable[[str | Path], ForwardingConfig] = load_forwarding_config,
        queue_factory: Callable[..., Any] = DurableDeliveryQueue,
        transport_factory: Callable[..., Any] = HTTPDeliveryTransport,
        worker_factory: Callable[..., Any] = DeliveryWorker,
        metric_enqueuer: Callable[..., MetricEnqueueStats] = enqueue_metric_points,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if reload_interval_s <= 0:
            raise ValueError("reload_interval_s must be positive")
        self.config_path = Path(config_path)
        self.spool_path = Path(spool_path)
        self.reload_interval_s = float(reload_interval_s)
        self._config_loader = config_loader
        self._queue_factory = queue_factory
        self._transport_factory = transport_factory
        self._worker_factory = worker_factory
        self._metric_enqueuer = metric_enqueuer
        self._monotonic_clock = monotonic_clock

        self.config = ForwardingConfig()
        self.queue: Any | None = None
        self.worker: Any | None = None
        self._started = False
        self._last_signature: object = object()
        self._next_reload = 0.0
        self._reloads = 0
        self._reload_failures = 0
        self._enqueue_failures = 0
        self._points_enqueued = 0
        self._points_dropped = 0
        self._last_config_error: str | None = None
        self._last_enqueue_error: str | None = None
        self._last_runtime_error: str | None = None

    def _signature(self):
        try:
            stat = self.config_path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ("error", type(exc).__name__, str(exc))
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _build_enabled(self, config: ForwardingConfig):
        queue = self._queue_factory(
            self.spool_path,
            max_items=config.queue_max_items,
            max_bytes=config.queue_max_bytes,
            max_payload_bytes=config.max_payload_bytes,
        )
        try:
            transport = self._transport_factory(
                config.endpoints,
                timeout_s=config.timeout_s,
                headers=config.header_mapping,
            )
            worker = self._worker_factory(
                queue,
                transport,
                batch_size=100,
                poll_interval_s=config.poll_interval_s,
                base_delay_s=config.base_delay_s,
                max_delay_s=config.max_delay_s,
                jitter=config.jitter,
            )
        except Exception:
            queue.close()
            raise
        return queue, worker

    def _install(self, config: ForwardingConfig) -> None:
        if config == self.config and (
            (not config.enabled and self.queue is None)
            or (config.enabled and self.queue is not None and self.worker is not None)
        ):
            return

        new_queue = None
        new_worker = None
        if config.enabled:
            new_queue, new_worker = self._build_enabled(config)

        old_queue = self.queue
        old_worker = self.worker
        old_stopped = True
        if old_worker is not None:
            old_stopped = bool(old_worker.stop(timeout_s=6.0))
            if not old_stopped:
                if new_queue is not None:
                    new_queue.close()
                raise RuntimeError("existing forwarding worker did not stop")

        try:
            if new_worker is not None:
                new_worker.start()
        except Exception:
            if old_worker is not None and old_stopped:
                try:
                    old_worker.start()
                except Exception:
                    pass
            if new_queue is not None:
                new_queue.close()
            raise

        self.queue = new_queue
        self.worker = new_worker
        self.config = config
        if old_queue is not None:
            old_queue.close()

    def reload(self, *, force: bool = False) -> bool:
        signature = self._signature()
        if not force and signature == self._last_signature:
            return False
        # Remember even invalid content so a bad file is not reparsed on every
        # Agent tick. Any edit changes mtime/size and triggers another attempt.
        self._last_signature = signature
        try:
            config = self._config_loader(self.config_path)
            self._install(config)
        except Exception as exc:
            self._reload_failures += 1
            self._last_config_error = str(exc)
            return False
        self._reloads += 1
        self._last_config_error = None
        self._last_runtime_error = None
        return True

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._next_reload = self._monotonic_clock()
        # Configuration failures are self-observed, not fatal to local Agent
        # collection. A valid future edit can recover without process restart.
        self.reload(force=True)
        self._next_reload = self._monotonic_clock() + self.reload_interval_s

    def tick(self) -> None:
        if not self._started:
            return
        now = self._monotonic_clock()
        if now < self._next_reload:
            return
        try:
            self.reload()
            self._last_runtime_error = None
        except Exception as exc:
            self._last_runtime_error = str(exc)
        finally:
            self._next_reload = now + self.reload_interval_s

    def enqueue(self, points: Iterable[CustomMetricPoint]) -> MetricEnqueueStats:
        values = tuple(points)
        if not values or not self.config.enabled or self.queue is None:
            return MetricEnqueueStats(len(values), 0, 0, 0)
        try:
            result = self._metric_enqueuer(
                self.queue,
                values,
                max_points_per_batch=self.config.batch_points,
                max_payload_bytes=self.config.max_payload_bytes,
            )
        except Exception as exc:
            self._enqueue_failures += 1
            self._last_enqueue_error = str(exc)
            return MetricEnqueueStats(len(values), 0, 0, len(values))
        self._points_enqueued += result.points_enqueued
        self._points_dropped += result.points_dropped
        self._last_enqueue_error = None
        return result

    def stop(self, timeout_s: float = 6.0) -> bool:
        self._started = False
        worker = self.worker
        queue = self.queue
        stopped = True
        if worker is not None:
            try:
                stopped = bool(worker.stop(timeout_s=timeout_s))
            except Exception as exc:
                self._last_runtime_error = str(exc)
                stopped = False
        if stopped and queue is not None:
            try:
                queue.close()
            except Exception as exc:
                self._last_runtime_error = str(exc)
                stopped = False
        if stopped:
            self.worker = None
            self.queue = None
        return stopped

    def snapshot(self) -> AgentForwardingSnapshot:
        queue_snapshot = None
        worker_snapshot = None
        runtime_error = self._last_runtime_error
        try:
            if self.queue is not None:
                queue_snapshot = self.queue.stats()
            if self.worker is not None:
                worker_snapshot = self.worker.snapshot()
        except Exception as exc:
            runtime_error = str(exc)
        return AgentForwardingSnapshot(
            running=self._started,
            enabled=self.config.enabled,
            reloads=self._reloads,
            reload_failures=self._reload_failures,
            enqueue_failures=self._enqueue_failures,
            points_enqueued=self._points_enqueued,
            points_dropped=self._points_dropped,
            last_config_error=self._last_config_error,
            last_enqueue_error=self._last_enqueue_error,
            last_runtime_error=runtime_error,
            config_path=str(self.config_path),
            spool_path=str(self.spool_path),
            queue=queue_snapshot,
            worker=worker_snapshot,
        )
