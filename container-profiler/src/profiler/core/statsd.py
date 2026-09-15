"""Small bounded StatsD/DogStatsD-compatible parser and local aggregator.

Supported metric types are gauge, count, histogram/timer and set. Datadog-style
``#tag:value`` suffixes and StatsD sample rates are understood. All dimensions
that can retain attacker/application-controlled data are explicitly bounded so
a local UDP sender cannot grow Agent memory without limit between flushes.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import statistics
import time
from typing import Callable

DEFAULT_MAX_SERIES = 10_000
DEFAULT_MAX_HISTOGRAM_VALUES = 4_096
DEFAULT_MAX_SET_VALUES = 4_096
MAX_LINE_CHARS = 8_192
MAX_METRIC_NAME_CHARS = 512
MAX_TAGS_PER_METRIC = 64
MAX_TAG_CHARS = 512


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
    """Parse one bounded StatsD/DogStatsD metric line."""
    text = line.strip()
    if len(text) > MAX_LINE_CHARS:
        raise StatsDParseError(f"metric line exceeds {MAX_LINE_CHARS} characters")
    if not text or ":" not in text:
        raise StatsDParseError("metric line must contain name:value")

    name, payload = text.split(":", 1)
    name = name.strip()
    if not name or any(ch.isspace() for ch in name):
        raise StatsDParseError("invalid metric name")
    if len(name) > MAX_METRIC_NAME_CHARS:
        raise StatsDParseError(f"metric name exceeds {MAX_METRIC_NAME_CHARS} characters")

    parts = payload.split("|")
    if len(parts) < 2:
        raise StatsDParseError("metric line must contain a type")
    raw_value, raw_type = parts[0], parts[1].lower()
    aliases = {
        "g": "gauge",
        "c": "count",
        "h": "histogram",
        "ms": "histogram",
        "s": "set",
    }
    metric_type = aliases.get(raw_type)
    if metric_type is None:
        raise StatsDParseError(f"unsupported metric type: {raw_type}")

    if metric_type == "set":
        value: float | str = raw_value
        if value == "":
            raise StatsDParseError("set value cannot be empty")
        if len(value) > MAX_LINE_CHARS:
            raise StatsDParseError("set value is too long")
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
            for raw_tag in extra[1:].split(","):
                tag = raw_tag.strip()
                if not tag:
                    continue
                if len(tag) > MAX_TAG_CHARS:
                    raise StatsDParseError(f"tag exceeds {MAX_TAG_CHARS} characters")
                tags.append(tag)
                if len(tags) > MAX_TAGS_PER_METRIC:
                    raise StatsDParseError(
                        f"metric has more than {MAX_TAGS_PER_METRIC} tags"
                    )
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
    """Aggregate a bounded number of metric series until ``flush``.

    New series are rejected once ``max_series`` is reached rather than evicting
    existing buckets. Histogram observations and set members have independent
    per-series caps. Existing gauges/counts continue updating even when the
    series cap is full. Drop counters are cumulative and intentionally survive
    flushes for Agent self-observability.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        *,
        max_series: int = DEFAULT_MAX_SERIES,
        max_histogram_values: int = DEFAULT_MAX_HISTOGRAM_VALUES,
        max_set_values: int = DEFAULT_MAX_SET_VALUES,
    ) -> None:
        if max_series <= 0 or max_histogram_values <= 0 or max_set_values <= 0:
            raise ValueError("StatsD aggregation limits must be positive")
        self._clock = clock
        self.max_series = int(max_series)
        self.max_histogram_values = int(max_histogram_values)
        self.max_set_values = int(max_set_values)
        self._buckets: dict[tuple[str, str, tuple[str, ...]], object] = {}
        self.parse_errors = 0
        self.received = 0
        self.accepted = 0
        self.dropped_series = 0
        self.dropped_histogram_values = 0
        self.dropped_set_values = 0

    def ingest(self, line: str) -> bool:
        try:
            metric = parse_statsd_line(line)
        except StatsDParseError:
            self.parse_errors += 1
            return False

        self.received += 1
        key = (metric.name, metric.metric_type, metric.tags)
        if key not in self._buckets and len(self._buckets) >= self.max_series:
            self.dropped_series += 1
            return False

        if metric.metric_type == "gauge":
            self._buckets[key] = float(metric.value)
        elif metric.metric_type == "count":
            scaled = float(metric.value) / metric.sample_rate
            self._buckets[key] = float(self._buckets.get(key, 0.0)) + scaled
        elif metric.metric_type == "histogram":
            bucket = self._buckets.setdefault(key, [])
            assert isinstance(bucket, list)
            if len(bucket) >= self.max_histogram_values:
                self.dropped_histogram_values += 1
                return False
            bucket.append((float(metric.value), metric.sample_rate))
        else:
            bucket = self._buckets.setdefault(key, set())
            assert isinstance(bucket, set)
            member = str(metric.value)
            if member not in bucket and len(bucket) >= self.max_set_values:
                self.dropped_set_values += 1
                return False
            bucket.add(member)
        self.accepted += 1
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

    @property
    def dropped_values(self) -> int:
        return self.dropped_histogram_values + self.dropped_set_values
