"""Encode normalized custom metrics into bounded durable forwarding batches."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from .custom_metrics import CustomMetricPoint
from .forwarding import DurableDeliveryQueue


@dataclass(frozen=True, slots=True)
class MetricEnqueueStats:
    input_points: int
    batches_enqueued: int
    points_enqueued: int
    points_dropped: int


def _point_payload(point: CustomMetricPoint) -> dict:
    payload = {
        "timestamp": float(point.timestamp),
        "name": point.name,
        "value": float(point.value),
        "tags": list(point.tags),
        "source": point.source,
    }
    if point.metric_type is not None:
        payload["type"] = point.metric_type
    if point.unit is not None:
        payload["unit"] = point.unit
    if point.target_key is not None:
        payload["target_key"] = point.target_key
    if point.container_id is not None:
        payload["container_id"] = point.container_id
    return payload


def _encode(points: list[dict]) -> bytes:
    return json.dumps(
        {"version": 1, "kind": "metrics", "points": points},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def enqueue_metric_points(
    queue: DurableDeliveryQueue,
    points: Iterable[CustomMetricPoint],
    *,
    max_points_per_batch: int = 250,
    max_payload_bytes: int = 1024 * 1024,
) -> MetricEnqueueStats:
    """Serialize points into bounded JSON payloads and durably enqueue them.

    A single point that cannot fit in one payload is dropped without blocking
    later points. Queue-level disk bounds remain a second independent line of
    defense when the upstream is unavailable for a long time.
    """
    if max_points_per_batch <= 0 or max_payload_bytes <= 0:
        raise ValueError("metric batch limits must be positive")

    values = tuple(points)
    encoded_points = [_point_payload(point) for point in values]
    batches = 0
    enqueued = 0
    dropped = 0
    current: list[dict] = []

    def flush() -> None:
        nonlocal batches, enqueued, dropped, current
        if not current:
            return
        count = len(current)
        payload = _encode(current)
        if queue.enqueue("metrics", payload):
            batches += 1
            enqueued += count
        else:
            dropped += count
        current = []

    for point in encoded_points:
        # First reject a pathological single point explicitly. This also avoids
        # repeatedly serializing an impossible batch around that point.
        single = _encode([point])
        if len(single) > max_payload_bytes:
            dropped += 1
            continue

        if not current:
            current = [point]
            continue

        if len(current) >= max_points_per_batch:
            flush()
            current = [point]
            continue

        candidate = _encode([*current, point])
        if len(candidate) > max_payload_bytes:
            flush()
            current = [point]
        else:
            current.append(point)

    flush()
    return MetricEnqueueStats(
        input_points=len(values),
        batches_enqueued=batches,
        points_enqueued=enqueued,
        points_dropped=dropped,
    )
