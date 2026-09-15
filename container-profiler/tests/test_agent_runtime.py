import unittest

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.host_monitor import HostStats
from profiler.core.storage import SQLiteTelemetryStore


class FakeHostWorker:
    def __init__(self, samples=()):
        self.items = list(samples)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout_s=2.0):
        self.stopped = True
        return True

    def drain(self, limit):
        result = self.items[:limit]
        del self.items[:limit]
        return tuple(result)

    def requeue_front(self, samples):
        values = list(samples)
        self.items[:0] = values
        return len(values)


class FakeSystemWorker:
    def __init__(self, points=()):
        self.items = list(points)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout_s=2.0):
        self.stopped = True
        return True

    def drain_points(self, limit):
        result = self.items[:limit]
        del self.items[:limit]
        return tuple(result)

    def requeue_points(self, points):
        values = list(points)
        self.items[:0] = values
        return len(values)


def host_sample(timestamp=100.0):
    return HostStats(
        timestamp=timestamp,
        cpu_percent=25.0,
        logical_cpus=8,
        memory_total_mb=16000.0,
        memory_available_mb=6000.0,
        memory_used_mb=10000.0,
        memory_percent=62.5,
        uptime_s=500.0,
        load_1=1.0,
        load_5=2.0,
        load_15=3.0,
    )


def system_point(timestamp=100.0):
    return CustomMetricPoint(
        timestamp=timestamp,
        name="system.net.bytes_rcvd",
        value=123.0,
        tags=("host:test", "device:eth0"),
        metric_type="gauge",
        unit="byte",
        source="system",
        target_key="host-network",
    )


class FailingStore:
    def __init__(self, *, host=False, system=False, retention=False):
        self.fail_host = host
        self.fail_system = system
        self.fail_retention = retention
        self.retention_calls = 0

    def append_host_samples(self, samples):
        if self.fail_host:
            raise OSError("host sqlite busy")
        return len(tuple(samples))

    def append_custom_metrics(self, points):
        if self.fail_system:
            raise OSError("system sqlite busy")
        return len(tuple(points))

    def prune_host_samples(self, **kwargs):
        self.retention_calls += 1
        if self.fail_retention:
            raise OSError("retention failed")
        return 0

    def prune_custom_metrics(self, **kwargs):
        self.retention_calls += 1
        if self.fail_retention:
            raise OSError("retention failed")
        return 0


class LocalAgentRuntimeTests(unittest.TestCase):
    def test_headless_runtime_persists_host_and_system_telemetry(self):
        with SQLiteTelemetryStore() as store:
            host = FakeHostWorker([host_sample()])
            system = FakeSystemWorker([system_point()])
            runtime = LocalAgentRuntime(store, host_worker=host, system_worker=system)
            runtime.start()
            runtime.run_once()
            snapshot = runtime.snapshot()
            self.assertTrue(snapshot.running)
            self.assertEqual(snapshot.host_samples_persisted, 1)
            self.assertEqual(snapshot.system_points_persisted, 1)
            self.assertEqual(len(store.query_host_samples(limit=10)), 1)
            rows = store.query_custom_metrics(source="system", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "system.net.bytes_rcvd")
            self.assertTrue(runtime.stop())
            self.assertTrue(host.stopped)
            self.assertTrue(system.stopped)

    def test_storage_failures_requeue_each_source_independently(self):
        store = FailingStore(host=True, system=True)
        host = FakeHostWorker([host_sample()])
        system = FakeSystemWorker([system_point()])
        runtime = LocalAgentRuntime(store, host_worker=host, system_worker=system)
        runtime.run_once()
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot.host_storage_failures, 1)
        self.assertEqual(snapshot.system_storage_failures, 1)
        self.assertIn("host sqlite busy", snapshot.last_host_storage_error)
        self.assertIn("system sqlite busy", snapshot.last_system_storage_error)
        self.assertEqual(len(host.items), 1)
        self.assertEqual(len(system.items), 1)

    def test_retention_has_own_deadline_and_no_every_tick_churn(self):
        now = [0.0]
        store = FailingStore()
        runtime = LocalAgentRuntime(
            store,
            host_worker=FakeHostWorker(),
            system_worker=FakeSystemWorker(),
            retention_interval_s=300.0,
            monotonic_clock=lambda: now[0],
            wall_clock=lambda: 10_000.0,
        )
        runtime.start()
        runtime.run_once()
        self.assertEqual(store.retention_calls, 0)
        now[0] = 300.0
        runtime.run_once()
        self.assertEqual(store.retention_calls, 2)
        runtime.run_once()
        self.assertEqual(store.retention_calls, 2)
        runtime.stop()

    def test_retention_failure_is_visible_and_next_deadline_advances(self):
        now = [0.0]
        store = FailingStore(retention=True)
        runtime = LocalAgentRuntime(
            store,
            host_worker=FakeHostWorker(),
            system_worker=FakeSystemWorker(),
            retention_interval_s=10.0,
            monotonic_clock=lambda: now[0],
        )
        runtime.start()
        now[0] = 10.0
        runtime.run_once()
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot.retention_failures, 1)
        self.assertIn("retention failed", snapshot.last_retention_error)
        runtime.run_once()
        self.assertEqual(runtime.snapshot().retention_failures, 1)
        runtime.stop()

    def test_stop_performs_final_drain_after_producers_stop(self):
        with SQLiteTelemetryStore() as store:
            host = FakeHostWorker([host_sample(200.0)])
            system = FakeSystemWorker([system_point(200.0)])
            runtime = LocalAgentRuntime(store, host_worker=host, system_worker=system)
            runtime.start()
            self.assertTrue(runtime.stop())
            self.assertEqual(runtime.snapshot().host_samples_persisted, 1)
            self.assertEqual(runtime.snapshot().system_points_persisted, 1)
            self.assertEqual(len(store.query_host_samples(limit=10)), 1)
            self.assertEqual(len(store.query_custom_metrics(source="system", limit=10)), 1)


if __name__ == "__main__":
    unittest.main()
