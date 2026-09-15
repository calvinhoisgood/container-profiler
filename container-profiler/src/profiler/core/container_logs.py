"""Resource-bounded Docker log collection for the headless Agent.

Collection deliberately uses bounded snapshot polling rather than one blocking
thread per container. Docker timestamps form the resume cursor; a small bounded
identity cache removes the overlap introduced by second-granularity ``since``
requests. Transient failures are retried from the last timestamped overlap while
all queues and replay state remain bounded; overload drops are explicitly counted.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import threading
import time
from typing import Any, Callable, Iterable

from .docker_monitor import DockerMonitor
from .log_storage import ContainerLogRecord
from .workload import DockerWorkloadInspector


@dataclass(frozen=True, slots=True)
class ContainerLogBufferStats:
    queued_records: int
    queued_bytes: int
    enqueued_records: int
    drained_records: int
    dropped_records: int
    oversized_records: int
    peak_records: int
    peak_bytes: int


class BoundedContainerLogBuffer:
    def __init__(
        self,
        *,
        max_records: int = 20_000,
        max_bytes: int = 16 * 1024 * 1024,
        max_record_bytes: int = 256 * 1024,
    ) -> None:
        if max_records <= 0 or max_bytes <= 0 or max_record_bytes <= 0:
            raise ValueError("log buffer limits must be positive")
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        self.max_record_bytes = int(max_record_bytes)
        self._items: deque[tuple[ContainerLogRecord, int]] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._enqueued = self._drained = self._dropped = self._oversized = 0
        self._peak_records = self._peak_bytes = 0

    @staticmethod
    def _size(record: ContainerLogRecord) -> int:
        return (
            128
            + len(record.container_id.encode("utf-8", errors="replace"))
            + len((record.container_name or "").encode("utf-8", errors="replace"))
            + len((record.docker_timestamp or "").encode("utf-8", errors="replace"))
            + len(record.message.encode("utf-8", errors="replace"))
            + sum(len(tag.encode("utf-8", errors="replace")) + 8 for tag in record.tags)
        )

    def append_many(self, records: Iterable[ContainerLogRecord]) -> int:
        accepted = 0
        with self._lock:
            for record in records:
                size = self._size(record)
                if size > self.max_record_bytes or size > self.max_bytes:
                    self._dropped += 1
                    self._oversized += 1
                    continue
                while self._items and (
                    len(self._items) >= self.max_records or self._bytes + size > self.max_bytes
                ):
                    _, old_size = self._items.popleft()
                    self._bytes -= old_size
                    self._dropped += 1
                self._items.append((record, size))
                self._bytes += size
                self._enqueued += 1
                accepted += 1
                self._peak_records = max(self._peak_records, len(self._items))
                self._peak_bytes = max(self._peak_bytes, self._bytes)
        return accepted

    def drain(self, limit: int = 1000) -> tuple[ContainerLogRecord, ...]:
        if limit <= 0:
            return ()
        output: list[ContainerLogRecord] = []
        with self._lock:
            while self._items and len(output) < int(limit):
                record, size = self._items.popleft()
                self._bytes -= size
                output.append(record)
            self._drained += len(output)
        return tuple(output)

    def requeue_front(self, records: Iterable[ContainerLogRecord]) -> int:
        values = list(records)
        restored = 0
        with self._lock:
            for record in reversed(values):
                size = self._size(record)
                if size > self.max_record_bytes or size > self.max_bytes:
                    self._dropped += 1
                    self._oversized += 1
                    continue
                while self._items and (
                    len(self._items) >= self.max_records or self._bytes + size > self.max_bytes
                ):
                    _, old_size = self._items.pop()
                    self._bytes -= old_size
                    self._dropped += 1
                self._items.appendleft((record, size))
                self._bytes += size
                restored += 1
            self._peak_records = max(self._peak_records, len(self._items))
            self._peak_bytes = max(self._peak_bytes, self._bytes)
        return restored

    def stats(self) -> ContainerLogBufferStats:
        with self._lock:
            return ContainerLogBufferStats(
                queued_records=len(self._items),
                queued_bytes=self._bytes,
                enqueued_records=self._enqueued,
                drained_records=self._drained,
                dropped_records=self._dropped,
                oversized_records=self._oversized,
                peak_records=self._peak_records,
                peak_bytes=self._peak_bytes,
            )


@dataclass(frozen=True, slots=True)
class ContainerLogWorkerSnapshot:
    running: bool
    collections: int
    discovery_failures: int
    sample_failures: int
    containers_seen: int
    containers_skipped: int
    records_collected: int
    duplicate_records: int
    untimestamped_records: int
    last_docker_error: str | None
    last_sample_errors: tuple[str, ...]
    buffer: ContainerLogBufferStats


def _timestamp_epoch(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return float(datetime.fromisoformat(text).timestamp())
    except (ValueError, OverflowError):
        return None


class ContainerLogWorker:
    """Poll bounded log snapshots for running containers in one background worker."""

    def __init__(
        self,
        monitor: Any | None = None,
        *,
        monitor_factory: Callable[[], Any] = DockerMonitor,
        inspector_factory: Callable[[Any], Any] = DockerWorkloadInspector,
        interval_s: float = 5.0,
        max_containers: int = 32,
        tail: int = 200,
        initial_lookback_s: float = 30.0,
        per_container_max_bytes: int = 256 * 1024,
        per_container_max_lines: int = 1000,
        dedupe_entries_per_container: int = 4096,
        max_records: int = 20_000,
        max_buffer_bytes: int = 16 * 1024 * 1024,
        max_record_bytes: int = 256 * 1024,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_s <= 0 or initial_lookback_s < 0:
            raise ValueError("log collection intervals must be valid")
        if max_containers <= 0 or tail <= 0 or dedupe_entries_per_container <= 0:
            raise ValueError("log collection bounds must be positive")
        if per_container_max_bytes <= 0 or per_container_max_lines <= 0:
            raise ValueError("per-container log bounds must be positive")
        self.monitor = monitor
        self.monitor_factory = monitor_factory
        self.inspector_factory = inspector_factory
        self.interval_s = float(interval_s)
        self.max_containers = int(max_containers)
        self.tail = int(tail)
        self.initial_lookback_s = float(initial_lookback_s)
        self.per_container_max_bytes = int(per_container_max_bytes)
        self.per_container_max_lines = int(per_container_max_lines)
        self.dedupe_entries_per_container = int(dedupe_entries_per_container)
        self.buffer = BoundedContainerLogBuffer(
            max_records=max_records,
            max_bytes=max_buffer_bytes,
            max_record_bytes=max_record_bytes,
        )
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._cursors: dict[str, float] = {}
        self._recent: dict[str, deque[tuple[str, str]]] = {}
        self._recent_counts: dict[str, dict[tuple[str, str], int]] = {}
        self._collections = 0
        self._discovery_failures = 0
        self._sample_failures = 0
        self._containers_seen = 0
        self._containers_skipped = 0
        self._records_collected = 0
        self._duplicate_records = 0
        self._untimestamped_records = 0
        self._last_docker_error: str | None = None
        self._last_sample_errors: tuple[str, ...] = ()

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _ensure_monitor(self) -> Any:
        if self.monitor is None:
            self.monitor = self.monitor_factory()
        return self.monitor

    def _remember_timestamped(self, container_id: str, key: tuple[str, str]) -> None:
        """Remember accepted log identities with bounded multiset semantics."""
        queue = self._recent.setdefault(container_id, deque())
        counts = self._recent_counts.setdefault(container_id, {})
        while len(queue) >= self.dedupe_entries_per_container:
            old = queue.popleft()
            remaining = counts.get(old, 0) - 1
            if remaining > 0:
                counts[old] = remaining
            else:
                counts.pop(old, None)
        queue.append(key)
        counts[key] = counts.get(key, 0) + 1

    def restore_state(
        self,
        cursors: dict[str, float],
        recent: dict[str, Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        """Restore only durable replay state before the worker starts."""
        with self._condition:
            if self.is_running():
                raise RuntimeError("cannot restore log replay state while worker is running")
            self._cursors = {
                str(container_id): float(timestamp)
                for container_id, timestamp in cursors.items()
                if str(container_id) and float(timestamp) >= 0
            }
            self._recent.clear()
            self._recent_counts.clear()
            for container_id, identities in (recent or {}).items():
                key = str(container_id)
                if key not in self._cursors:
                    continue
                values = list(identities)[-self.dedupe_entries_per_container :]
                for timestamp, message in values:
                    self._remember_timestamped(key, (str(timestamp), str(message)))

    def collect_once(self) -> bool:
        try:
            monitor = self._ensure_monitor()
            containers = list(monitor.list_containers(all=False))
        except Exception as exc:
            with self._condition:
                self._discovery_failures += 1
                self._last_docker_error = str(exc)
            return False

        discovery_error = getattr(monitor, "last_error", None)
        if discovery_error:
            with self._condition:
                self._discovery_failures += 1
                self._last_docker_error = str(discovery_error)
            return False

        running = sorted(
            (item for item in containers if str(getattr(item, "status", "")).lower() == "running"),
            key=lambda item: str(getattr(item, "id", "")),
        )
        total = len(running)
        skipped = max(0, total - self.max_containers)
        running = running[: self.max_containers]
        active_ids = {str(item.id) for item in running}
        now = float(self.wall_clock())
        sample_errors: list[str] = []
        accepted_records: list[ContainerLogRecord] = []
        duplicates = 0
        untimestamped = 0

        client = getattr(monitor, "client", None)
        try:
            inspector = self.inspector_factory(client)
        except Exception as exc:
            with self._condition:
                self._discovery_failures += 1
                self._last_docker_error = f"log inspector unavailable: {exc}"
            return False

        for container in running:
            container_id = str(container.id)
            cursor = self._cursors.get(container_id)
            since = int(max(0.0, (cursor - 1.0) if cursor is not None else now - self.initial_lookback_s))
            try:
                lines = inspector.get_logs(
                    container_id,
                    tail=self.tail,
                    since=since,
                    max_bytes=self.per_container_max_bytes,
                    max_lines=self.per_container_max_lines,
                )
            except Exception as exc:
                lines = None
                try:
                    setattr(inspector, "last_error", str(exc))
                except Exception:
                    pass
            if lines is None:
                detail = getattr(inspector, "last_error", None) or "Docker logs unavailable"
                sample_errors.append(f"{container_id[:12]}: {detail}")
                continue

            newest = cursor
            overlap_counts = dict(self._recent_counts.get(container_id, {}))
            for line in lines:
                docker_timestamp = getattr(line, "timestamp", None)
                message = str(getattr(line, "message", ""))
                epoch = _timestamp_epoch(docker_timestamp)
                if docker_timestamp and epoch is not None:
                    key = (docker_timestamp, message)
                    remaining = overlap_counts.get(key, 0)
                    if remaining > 0:
                        duplicates += 1
                        if remaining == 1:
                            overlap_counts.pop(key, None)
                        else:
                            overlap_counts[key] = remaining - 1
                        continue
                    self._remember_timestamped(container_id, key)
                    newest = epoch if newest is None else max(newest, epoch)
                    timestamp = epoch
                else:
                    untimestamped += 1
                    timestamp = now
                accepted_records.append(
                    ContainerLogRecord(
                        timestamp=timestamp,
                        docker_timestamp=docker_timestamp,
                        container_id=container_id,
                        container_name=str(getattr(container, "name", "") or "") or None,
                        message=message,
                        tags=tuple(getattr(container, "tags", ()) or ()),
                    )
                )
            if newest is not None:
                self._cursors[container_id] = newest

        for stale in set(self._cursors) - active_ids:
            self._cursors.pop(stale, None)
            self._recent.pop(stale, None)
            self._recent_counts.pop(stale, None)

        accepted = self.buffer.append_many(accepted_records)
        with self._condition:
            self._collections += 1
            self._containers_seen = total
            self._containers_skipped += skipped
            self._sample_failures += len(sample_errors)
            self._records_collected += accepted
            self._duplicate_records += duplicates
            self._untimestamped_records += untimestamped
            self._last_docker_error = None
            self._last_sample_errors = tuple(sample_errors[:32])
        return True

    def start(self) -> None:
        with self._condition:
            if self.is_running():
                return
            self._stop_requested = False
            self._thread = threading.Thread(target=self._run, name="container-log-collector", daemon=True)
            self._thread.start()

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
            next_due = max(next_due + self.interval_s, finished + self.interval_s)

    def drain_records(self, limit: int = 1000) -> tuple[ContainerLogRecord, ...]:
        return self.buffer.drain(limit)

    def requeue_records(self, records: Iterable[ContainerLogRecord]) -> int:
        return self.buffer.requeue_front(records)

    def snapshot(self) -> ContainerLogWorkerSnapshot:
        with self._condition:
            return ContainerLogWorkerSnapshot(
                running=self.is_running(),
                collections=self._collections,
                discovery_failures=self._discovery_failures,
                sample_failures=self._sample_failures,
                containers_seen=self._containers_seen,
                containers_skipped=self._containers_skipped,
                records_collected=self._records_collected,
                duplicate_records=self._duplicate_records,
                untimestamped_records=self._untimestamped_records,
                last_docker_error=self._last_docker_error,
                last_sample_errors=self._last_sample_errors,
                buffer=self.buffer.stats(),
            )
