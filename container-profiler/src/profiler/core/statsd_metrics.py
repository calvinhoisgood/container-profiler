"""Bridge the local StatsD listener into the Agent custom-metric pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .custom_metrics import BoundedMetricBuffer, CustomMetricPoint, MetricBufferStats
from .statsd import AggregatedMetric, StatsDAggregator
from .statsd_server import StatsDUDPServer


def normalize_statsd_metrics(
    metrics: Iterable[AggregatedMetric],
) -> tuple[CustomMetricPoint, ...]:
    """Normalize one aggregator flush without inventing unsupported semantics."""
    points: list[CustomMetricPoint] = []
    for metric in metrics:
        common = dict(
            timestamp=float(metric.timestamp),
            tags=tuple(metric.tags),
            source="dogstatsd",
            target_key="local-dogstatsd",
        )
        if metric.metric_type == "gauge":
            points.append(
                CustomMetricPoint(
                    name=metric.name,
                    value=float(metric.values["value"]),
                    metric_type="gauge",
                    **common,
                )
            )
        elif metric.metric_type == "count":
            points.append(
                CustomMetricPoint(
                    name=metric.name,
                    value=float(metric.values["value"]),
                    metric_type="count",
                    **common,
                )
            )
        elif metric.metric_type == "set":
            points.append(
                CustomMetricPoint(
                    name=f"{metric.name}.cardinality",
                    value=float(metric.values["count"]),
                    metric_type="gauge",
                    **common,
                )
            )
        else:
            # Keep the aggregate explicit. A percentile is a gauge; count/sum are
            # flush-window aggregates rather than cumulative host counters.
            for suffix in ("count", "min", "max", "sum", "mean", "p50", "p95"):
                value = metric.values.get(suffix)
                if value is None:
                    continue
                points.append(
                    CustomMetricPoint(
                        name=f"{metric.name}.{suffix}",
                        value=float(value),
                        metric_type=(
                            "count" if suffix in {"count", "sum"} else "gauge"
                        ),
                        **common,
                    )
                )
    return tuple(points)


@dataclass(frozen=True, slots=True)
class StatsDMetricsSnapshot:
    running: bool
    datagrams_received: int
    lines_received: int
    decode_errors: int
    parse_errors: int
    flush_count: int
    last_error: str | None
    buffer: MetricBufferStats
    accepted_lines: int = 0
    pending_series: int = 0
    dropped_series: int = 0
    dropped_histogram_values: int = 0
    dropped_set_values: int = 0


class StatsDMetricsWorker:
    """Own a loopback UDP listener and bounded aggregation/persistence queues."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8125,
        flush_interval_s: float = 10.0,
        max_points: int = 20_000,
        max_bytes: int = 16 * 1024 * 1024,
        max_series: int = 10_000,
        max_histogram_values: int = 4_096,
        max_set_values: int = 4_096,
        server: StatsDUDPServer | None = None,
    ) -> None:
        self.buffer = BoundedMetricBuffer(max_points=max_points, max_bytes=max_bytes)
        self.server = server or StatsDUDPServer(
            host=host,
            port=port,
            flush_interval_s=flush_interval_s,
            aggregator=StatsDAggregator(
                max_series=max_series,
                max_histogram_values=max_histogram_values,
                max_set_values=max_set_values,
            ),
            on_flush=self._on_flush,
        )
        if server is not None:
            self.server.on_flush = self._on_flush

    def _on_flush(self, metrics: list[AggregatedMetric]) -> None:
        self.buffer.append_many(normalize_statsd_metrics(metrics))

    def start(self) -> tuple[str, int]:
        return self.server.start()

    def stop(self, timeout_s: float = 2.0) -> bool:
        return self.server.stop(timeout_s)

    def drain_points(self, limit: int = 1000) -> tuple[CustomMetricPoint, ...]:
        return self.buffer.drain(limit)

    def requeue_points(self, points: Iterable[CustomMetricPoint]) -> int:
        return self.buffer.requeue_front(points)

    def snapshot(self) -> StatsDMetricsSnapshot:
        aggregator = self.server.aggregator
        return StatsDMetricsSnapshot(
            running=self.server.is_running,
            datagrams_received=self.server.datagrams_received,
            lines_received=self.server.lines_received,
            decode_errors=self.server.decode_errors,
            parse_errors=aggregator.parse_errors,
            flush_count=self.server.flush_count,
            last_error=self.server.last_error,
            buffer=self.buffer.stats(),
            accepted_lines=aggregator.accepted,
            pending_series=aggregator.pending_series,
            dropped_series=aggregator.dropped_series,
            dropped_histogram_values=aggregator.dropped_histogram_values,
            dropped_set_values=aggregator.dropped_set_values,
        )
