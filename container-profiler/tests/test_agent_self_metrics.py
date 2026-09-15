from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_self_metrics import normalize_agent_self_metrics
from profiler.core.agent_runtime import LocalAgentRuntime


class SnapshotNormalizationTests(unittest.TestCase):
    def test_flattens_core_and_nested_health(self):
        buffer = SimpleNamespace(dropped_points=4)
        snapshot = SimpleNamespace(
            ticks=9,
            host_samples_persisted=10,
            system_points_persisted=20,
            container_points_persisted=30,
            statsd_points_persisted=40,
            openmetrics_points_persisted=50,
            host_storage_failures=1,
            system_storage_failures=2,
            container_storage_failures=3,
            statsd_storage_failures=4,
            openmetrics_storage_failures=5,
            retention_failures=6,
            statsd=SimpleNamespace(
                parse_errors=7,
                dropped_series=8,
                dropped_histogram_values=9,
                dropped_set_values=10,
                buffer=buffer,
            ),
            openmetrics=SimpleNamespace(
                refresh_failures=11,
                target_count=12,
                worker=SimpleNamespace(buffer=buffer),
            ),
            containers=SimpleNamespace(
                discovery_failures=13,
                sample_failures=14,
                containers_seen=15,
                buffer=buffer,
            ),
            forwarding=SimpleNamespace(
                reload_failures=16,
                enqueue_failures=17,
                points_enqueued=18,
                points_dropped=19,
                queue=SimpleNamespace(
                    queued_items=20,
                    queued_bytes=21,
                    dropped_items=22,
                    failed_attempts=23,
                ),
            ),
        )
        points = normalize_agent_self_metrics(
            snapshot,
            timestamp=100.0,
            hostname="node-a",
        )
        by_name = {}
        for point in points:
            by_name.setdefault(point.name, []).append(point)
        self.assertEqual(by_name["container_profiler.agent.ticks"][0].value, 9.0)
        persisted = by_name["container_profiler.agent.persisted"]
        self.assertEqual(len(persisted), 5)
        self.assertTrue(all(point.source == "agent" for point in points))
        self.assertTrue(all("host:node-a" in point.tags for point in points))
        self.assertEqual(
            by_name["container_profiler.forwarding.queued_items"][0].value,
            20.0,
        )


class EmptyHost:
    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def drain(self, limit):
        return ()

    def requeue_front(self, items):
        return len(tuple(items))


class EmptyMetric:
    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def drain_points(self, limit):
        return ()

    def requeue_points(self, items):
        return len(tuple(items))


class Store:
    def __init__(self):
        self.metric_batches = []

    def append_host_samples(self, items):
        return len(tuple(items))

    def append_custom_metrics(self, items):
        values = tuple(items)
        self.metric_batches.append(values)
        return len(values)

    def prune_host_samples(self, **kwargs):
        return 0

    def prune_custom_metrics(self, **kwargs):
        return 0


class AgentSelfMetricRuntimeTests(unittest.TestCase):
    def test_runtime_emits_self_metrics_on_independent_cadence(self):
        clock = [0.0]
        store = Store()
        runtime = LocalAgentRuntime(
            store,
            host_worker=EmptyHost(),
            system_worker=EmptyMetric(),
            self_metrics_interval_s=10.0,
            hostname=lambda: "test-host",
            wall_clock=lambda: 123.0,
            monotonic_clock=lambda: clock[0],
        )
        runtime.start()
        runtime.run_once()
        self.assertEqual(store.metric_batches, [])
        clock[0] = 10.0
        runtime.run_once()
        self.assertEqual(len(store.metric_batches), 1)
        names = {point.name for point in store.metric_batches[0]}
        self.assertIn("container_profiler.agent.ticks", names)
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot.self_points_persisted, len(store.metric_batches[0]))
        self.assertEqual(snapshot.self_storage_failures, 0)
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
