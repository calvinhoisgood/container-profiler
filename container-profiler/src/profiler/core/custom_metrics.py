"""Normalized custom metrics and a bounded in-memory delivery buffer."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
import threading
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class CustomMetricPoint:
    timestamp: float
    name: str
    value: float
    tags: tuple[str, ...] = ()
    metric_type: str | None = None
    unit: str | None = None
    source: str = "openmetrics"
    target_key: str | None = None
    container_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricBufferStats:
    queued_points: int
    queued_bytes: int
    enqueued_points: int
    drained_points: int
    dropped_points: int
    oversized_points: int
    peak_points: int
    peak_bytes: int


def _point_size(point: CustomMetricPoint) -> int:
    # A deterministic conservative accounting estimate. Python object overhead
    # is implementation-specific, so count encoded payload plus fixed metadata.
    return (
        128
        + len(point.name.encode("utf-8"))
        + sum(len(tag.encode("utf-8")) + 8 for tag in point.tags)
        + (len(point.unit.encode("utf-8")) if point.unit else 0)
        + (len(point.metric_type.encode("utf-8")) if point.metric_type else 0)
    )


class BoundedMetricBuffer:
    """Thread-safe point buffer bounded by both count and estimated bytes.

    Backpressure uses drop-oldest semantics: recent telemetry stays useful to
    the Explorer while an overloaded consumer cannot grow memory without bound.
    Oversized single points are rejected explicitly and counted separately.
    """

    def __init__(
        self,
        *,
        max_points: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
        max_point_bytes: int = 64 * 1024,
    ) -> None:
        if max_points <= 0 or max_bytes <= 0 or max_point_bytes <= 0:
            raise ValueError("metric buffer limits must be positive")
        self.max_points = max_points
        self.max_bytes = max_bytes
        self.max_point_bytes = max_point_bytes
        self._items: deque[tuple[CustomMetricPoint, int]] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._enqueued = 0
        self._drained = 0
        self._dropped = 0
        self._oversized = 0
        self._peak_points = 0
        self._peak_bytes = 0

    def append_many(self, points: Iterable[CustomMetricPoint]) -> int:
        accepted = 0
        with self._lock:
            for point in points:
                size = _point_size(point)
                if size > self.max_point_bytes or size > self.max_bytes:
                    self._dropped += 1
                    self._oversized += 1
                    continue
                while self._items and (
                    len(self._items) >= self.max_points
                    or self._bytes + size > self.max_bytes
                ):
                    _, old_size = self._items.popleft()
                    self._bytes -= old_size
                    self._dropped += 1
                self._items.append((point, size))
                self._bytes += size
                self._enqueued += 1
                accepted += 1
                self._peak_points = max(self._peak_points, len(self._items))
                self._peak_bytes = max(self._peak_bytes, self._bytes)
        return accepted

    def drain(self, limit: int = 1000) -> tuple[CustomMetricPoint, ...]:
        if limit <= 0:
            return ()
        result: list[CustomMetricPoint] = []
        with self._lock:
            while self._items and len(result) < limit:
                point, size = self._items.popleft()
                self._bytes -= size
                result.append(point)
            self._drained += len(result)
        return tuple(result)

    def requeue_front(self, points: Iterable[CustomMetricPoint]) -> int:
        """Return failed-delivery points to the front while preserving bounds."""
        values = list(points)
        restored = 0
        with self._lock:
            for point in reversed(values):
                size = _point_size(point)
                if size > self.max_point_bytes or size > self.max_bytes:
                    self._dropped += 1
                    self._oversized += 1
                    continue
                while self._items and (
                    len(self._items) >= self.max_points
                    or self._bytes + size > self.max_bytes
                ):
                    _, old_size = self._items.pop()
                    self._bytes -= old_size
                    self._dropped += 1
                self._items.appendleft((point, size))
                self._bytes += size
                restored += 1
            self._peak_points = max(self._peak_points, len(self._items))
            self._peak_bytes = max(self._peak_bytes, self._bytes)
        return restored

    def stats(self) -> MetricBufferStats:
        with self._lock:
            return MetricBufferStats(
                queued_points=len(self._items),
                queued_bytes=self._bytes,
                enqueued_points=self._enqueued,
                drained_points=self._drained,
                dropped_points=self._dropped,
                oversized_points=self._oversized,
                peak_points=self._peak_points,
                peak_bytes=self._peak_bytes,
            )


def normalize_openmetrics_samples(
    target: Any,
    result: Any,
    *,
    collected_at: float,
) -> tuple[CustomMetricPoint, ...]:
    """Convert one raw OpenMetrics scrape into the shared custom-metric model.

    Metric aliases are matched against the raw exposition name before the
    configured namespace is applied. Target-level tags are merged with sample
    labels while preserving first-seen order.
    """
    compiled_renames = tuple(
        (re.compile(pattern), destination)
        for pattern, destination in getattr(target, "metric_renames", ())
    )
    points: list[CustomMetricPoint] = []
    namespace = getattr(target, "namespace", "")
    target_tags = tuple(getattr(target, "tags", ()))

    for sample in getattr(result, "samples", ()):
        raw_name = str(sample.name)
        name = raw_name
        for pattern, destination in compiled_renames:
            if pattern.fullmatch(raw_name):
                name = destination
                break
        if namespace:
            name = f"{namespace}.{name}"

        tags = tuple(
            dict.fromkeys((*target_tags, *tuple(getattr(sample, "tags", ()))))
        )
        timestamp_ms = getattr(sample, "timestamp_ms", None)
        timestamp = (
            float(timestamp_ms) / 1000.0
            if timestamp_ms is not None
            else float(collected_at)
        )
        points.append(
            CustomMetricPoint(
                timestamp=timestamp,
                name=name,
                value=float(sample.value),
                tags=tags,
                metric_type=getattr(sample, "metric_type", None),
                unit=getattr(sample, "unit", None),
                source="openmetrics",
                target_key=getattr(target, "key", None),
                container_id=getattr(target, "container_id", None),
            )
        )
    return tuple(points)
