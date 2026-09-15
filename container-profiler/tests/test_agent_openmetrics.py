import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_openmetrics import AgentOpenMetricsWorker
from profiler.core.agent_runtime import LocalAgentRuntime


class FakeMonitor:
    def __init__(self, containers=(), error=None):
        self.containers = list(containers)
        self.last_error = error
        self.calls = 0

    def list_containers(self, all=True):
        self.calls += 1
        return list(self.containers)


class FakeMetricWorker:
    def __init__(self, points=()):
        self.targets = ("old",)
        self.items = list(points)
        self.started = False
        self.stopped = False
        self.replace_calls = []

    def start(self):
        self.started = True

    def stop(self, timeout_s=2.0):
        self.stopped = True
        return True

    def replace_targets(self, targets):
        self.targets = tuple(targets)
        self.replace_calls.append(self.targets)

    def drain_points(self, limit):
        result = self.items[:limit]
        del self.items[:limit]
        return tuple(result)

    def requeue_points(self, points):
        values = list(points)
        self.items[:0] = values
        return len(values)

    def snapshot(self):
        return {"running": self.started and not self.stopped}


class EmptyHostWorker:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout_s=2.0):
        self.stopped = True
        return True

    def drain(self, limit):
        return ()

    def requeue_front(self, values):
        return len(tuple(values))


class EmptyMetricWorker(FakeMetricWorker):
    def __init__(self):
        super().__init__(())


class FakeStore:
    def __init__(self, fail_metrics=False):
        self.fail_metrics = fail_metrics
        self.points = []

    def append_host_samples(self, samples):
        return len(tuple(samples))

    def append_custom_metrics(self, points):
        if self.fail_metrics:
            raise OSError("sqlite busy")
        values = list(points)
        self.points.extend(values)
        return len(values)

    def prune_host_samples(self, **kwargs):
        return 0

    def prune_custom_metrics(self, **kwargs):
        return 0


class AgentOpenMetricsWorkerTests(unittest.TestCase):
    def test_successful_empty_scan_removes_targets(self):
        metrics = FakeMetricWorker()
        worker = AgentOpenMetricsWorker(
            monitor=FakeMonitor(),
            metrics_worker=metrics,
            resolver=lambda containers: SimpleNamespace(targets=(), errors=("warning",)),
            wall_clock=lambda: 123.0,
        )
        self.assertTrue(worker.refresh_once())
        self.assertEqual(metrics.targets, ())
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.refreshes, 1)
        self.assertEqual(snapshot.target_count, 0)
        self.assertEqual(snapshot.discovery_errors, ("warning",))
        self.assertEqual(snapshot.last_refresh_at, 123.0)

    def test_docker_error_preserves_last_known_good_targets(self):
        metrics = FakeMetricWorker()
        worker = AgentOpenMetricsWorker(
            monitor=FakeMonitor(error="daemon unavailable"),
            metrics_worker=metrics,
            resolver=lambda containers: SimpleNamespace(targets=("new",), errors=()),
        )
        self.assertFalse(worker.refresh_once())
        self.assertEqual(metrics.targets, ("old",))
        self.assertEqual(metrics.replace_calls, [])
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.refresh_failures, 1)
        self.assertIn("daemon unavailable", snapshot.last_docker_error)

    def test_monitor_creation_is_lazy_and_thread_is_stoppable(self):
        metrics = FakeMetricWorker()
        created = []

        def factory():
            created.append(True)
            return FakeMonitor([object()])

        worker = AgentOpenMetricsWorker(
            monitor_factory=factory,
            metrics_worker=metrics,
            resolver=lambda containers: SimpleNamespace(targets=("target",), errors=()),
            discovery_interval_s=0.01,
        )
        self.assertEqual(created, [])
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and not metrics.replace_calls:
            time.sleep(0.005)
        self.assertTrue(created)
        self.assertTrue(metrics.replace_calls)
        self.assertTrue(worker.is_running())
        self.assertTrue(worker.stop(timeout_s=1.0))
        self.assertFalse(worker.is_running())
        self.assertTrue(metrics.stopped)


class AgentRuntimeOpenMetricsTests(unittest.TestCase):
    def test_openmetrics_points_are_persisted_and_visible_in_snapshot(self):
        point = SimpleNamespace(name="requests")
        openmetrics = FakeMetricWorker((point,))
        runtime = LocalAgentRuntime(
            FakeStore(),
            host_worker=EmptyHostWorker(),
            system_worker=EmptyMetricWorker(),
            openmetrics_worker=openmetrics,
        )
        runtime.start()
        runtime.run_once()
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot.openmetrics_points_persisted, 1)
        self.assertEqual(snapshot.openmetrics_storage_failures, 0)
        self.assertIsNotNone(snapshot.openmetrics)
        self.assertTrue(runtime.stop())
        self.assertTrue(openmetrics.stopped)

    def test_openmetrics_storage_failure_requeues_without_statsd_pollution(self):
        point = SimpleNamespace(name="requests")
        openmetrics = FakeMetricWorker((point,))
        runtime = LocalAgentRuntime(
            FakeStore(fail_metrics=True),
            host_worker=EmptyHostWorker(),
            system_worker=EmptyMetricWorker(),
            openmetrics_worker=openmetrics,
        )
        runtime.run_once()
        snapshot = runtime.snapshot()
        self.assertEqual(len(openmetrics.items), 1)
        self.assertEqual(snapshot.openmetrics_storage_failures, 1)
        self.assertEqual(snapshot.statsd_storage_failures, 0)
        self.assertIn("sqlite busy", snapshot.last_openmetrics_storage_error)

    def test_openmetrics_start_failure_unwinds_started_workers(self):
        class BrokenOpenMetrics(FakeMetricWorker):
            def start(self):
                raise RuntimeError("openmetrics failed")

        host = EmptyHostWorker()
        system = EmptyMetricWorker()
        statsd = EmptyMetricWorker()
        runtime = LocalAgentRuntime(
            FakeStore(),
            host_worker=host,
            system_worker=system,
            statsd_worker=statsd,
            openmetrics_worker=BrokenOpenMetrics(),
        )
        with self.assertRaisesRegex(RuntimeError, "openmetrics failed"):
            runtime.start()
        self.assertTrue(host.stopped)
        self.assertTrue(system.stopped)
        self.assertTrue(statsd.stopped)


if __name__ == "__main__":
    unittest.main()
