from __future__ import annotations

import unittest

from profiler.core.statsd import AggregatedMetric, StatsDAggregator
from profiler.core.statsd_metrics import StatsDMetricsWorker, normalize_statsd_metrics
from profiler.core.statsd_server import StatsDUDPServer


class StatsDMetricsTests(unittest.TestCase):
    def test_normalizes_gauge_count_set_and_histogram(self):
        metrics = [
            AggregatedMetric("temperature", "gauge", ("env:test",), 10.0, {"value": 7.5}),
            AggregatedMetric("requests", "count", (), 10.0, {"value": 3.0}),
            AggregatedMetric("users", "set", (), 10.0, {"count": 2.0}),
            AggregatedMetric("latency", "histogram", (), 10.0, {
                "count": 2.0, "min": 1.0, "max": 3.0, "sum": 4.0,
                "mean": 2.0, "p50": 2.0, "p95": 2.9,
            }),
        ]
        points = normalize_statsd_metrics(metrics)
        by_name = {point.name: point for point in points}
        self.assertEqual(by_name["temperature"].tags, ("env:test",))
        self.assertEqual(by_name["requests"].metric_type, "count")
        self.assertEqual(by_name["users.cardinality"].value, 2.0)
        self.assertEqual(by_name["latency.p95"].value, 2.9)
        self.assertEqual(by_name["latency.count"].metric_type, "count")
        self.assertTrue(all(point.source == "dogstatsd" for point in points))

    def test_flush_enters_bounded_buffer(self):
        aggregator = StatsDAggregator(clock=lambda: 12.0)
        server = StatsDUDPServer(port=0, aggregator=aggregator)
        worker = StatsDMetricsWorker(server=server, max_points=2, max_bytes=4096)
        aggregator.ingest("a:1|g")
        aggregator.ingest("b:2|g")
        aggregator.ingest("c:3|g")
        server._emit_flush()
        points = worker.drain_points(10)
        self.assertEqual([point.name for point in points], ["b", "c"])
        self.assertEqual(worker.snapshot().buffer.dropped_points, 1)

    def test_requeue_preserves_failed_persistence_points(self):
        aggregator = StatsDAggregator(clock=lambda: 12.0)
        server = StatsDUDPServer(port=0, aggregator=aggregator)
        worker = StatsDMetricsWorker(server=server)
        aggregator.ingest("jobs:4|c|#service:worker")
        server._emit_flush()
        points = worker.drain_points(10)
        self.assertEqual(len(points), 1)
        worker.requeue_points(points)
        restored = worker.drain_points(10)
        self.assertEqual(restored, points)


if __name__ == "__main__":
    unittest.main()
