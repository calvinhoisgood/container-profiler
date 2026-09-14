"""Small StatsD/DogStatsD-compatible parser and local aggregator.

This is intentionally transport-agnostic. A UDP/UDS listener can feed lines into
``StatsDAggregator`` while tests and future service processes can use it directly.
Supported metric types: gauge, count, histogram/timer, and set. Datadog-style
``#tag:value`` suffixes and StatsD sample rates are understood.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable
import statistics
import time


@dataclass(frozen=True, slots=True)
class StatsDMetric:
    name: str
    value: float | str
    metric_type: str
    tags: tuple[str, ...] = ()
    sample_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class AggregatedMetric:
    name: str
    metric_type: str
    tags: tuple[str, ...]
    timestamp: float
    values: dict[str, float]


class StatsDParseError(ValueError):
    pass


def parse_statsd_line(line: str) -> StatsDMetric:
    """Parse one StatsD/DogStatsD metric line."""
    text = line.strip()
    if not text or ":" not in text:
        raise StatsDParseError("metric line must contain name:value")

    name, payload = text.split(":", 1)
    name = name.strip()
    if not name or any(ch.isspace() for ch in name):
        raise StatsDParseError("invalid metric name")

    parts = payload.split("|")
    if len(parts) < 2:
        raise StatsDParseError("metric line must contain a type")
    raw_value, raw_type = parts[0], parts[1].lower()
    aliases = {"g": "gauge", "c": "count", "h": "histogram", "ms": "histogram", "s": "set"}
    metric_type = aliases.get(raw_type)
    if metric_type is None:
        raise StatsDParseError(f"unsupported metric type: {raw_type}")

    if metric_type == "set":
        value: float | str = raw_value
        if value == "":
            raise StatsDParseError("set value cannot be empty")
    else:
        try:
            numeric = float(raw_value)
        except ValueError as exc:
            raise StatsDParseError("metric value must be numeric") from exc
        if not isfinite(numeric):
            raise StatsDParseError("metric value must be finite")
        value = numeric

    sample_rate = 1.0
    tags: list[str] = []
    for extra in parts[2:]:
        if extra.startswith("@"):
            try:
                sample_rate = float(extra[1:])
            except ValueError as exc:
                raise StatsDParseError("invalid sample rate") from exc
            if not (0.0 < sample_rate <= 1.0):
                raise StatsDParseError("sample rate must be in (0, 1]")
        elif extra.startswith("#"):
            tags.extend(tag.strip() for tag in extra[1:].split(",") if tag.strip())
        elif extra:
            raise StatsDParseError(f"unsupported extension: {extra}")

    return StatsDMetric(
        name=name,
        value=value,
        metric_type=metric_type,
        tags=tuple(sorted(set(tags))),
        sample_rate=sample_rate,
    )


class StatsDAggregator:
    """Aggregate metric packets by (name, type, tags) until ``flush``."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, str, tuple[str, ...]], object] = {}
        self.parse_errors = 0
        self.received = 0

    def ingest(self, line: str) -> bool:
        try:
            metric = parse_statsd_line(line)
        except StatsDParseError:
            self.parse_errors += 1
            return False

        self.received += 1
        key = (metric.name, metric.metric_type, metric.tags)
        if metric.metric_type == "gauge":
            self._buckets[key] = float(metric.value)
        elif metric.metric_type == "count":
            scaled = float(metric.value) / metric.sample_rate
            self._buckets[key] = float(self._buckets.get(key, 0.0)) + scaled
        elif metric.metric_type == "histogram":
            bucket = self._buckets.setdefault(key, [])
            assert isinstance(bucket, list)
            bucket.append((float(metric.value), metric.sample_rate))
        else:
            bucket = self._buckets.setdefault(key, set())
            assert isinstance(bucket, set)
            bucket.add(str(metric.value))
        return True

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * q
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        frac = rank - low
        return ordered[low] * (1 - frac) + ordered[high] * frac

    def flush(self, timestamp: float | None = None) -> list[AggregatedMetric]:
        ts = self._clock() if timestamp is None else timestamp
        result: list[AggregatedMetric] = []
        for (name, metric_type, tags), bucket in sorted(self._buckets.items()):
            if metric_type == "gauge":
                values = {"value": float(bucket)}
            elif metric_type == "count":
                values = {"value": float(bucket)}
            elif metric_type == "set":
                assert isinstance(bucket, set)
                values = {"count": float(len(bucket))}
            else:
                assert isinstance(bucket, list)
                observations = [value for value, _ in bucket]
                effective_count = sum(1.0 / rate for _, rate in bucket)
                values = {
                    "count": effective_count,
                    "min": min(observations),
                    "max": max(observations),
                    "sum": sum(observations),
                    "mean": statistics.fmean(observations),
                    "p50": self._percentile(observations, 0.50),
                    "p95": self._percentile(observations, 0.95),
                }
            result.append(AggregatedMetric(name, metric_type, tags, ts, values))

        self._buckets.clear()
        return result

    @property
    def pending_series(self) -> int:
        return len(self._buckets)
