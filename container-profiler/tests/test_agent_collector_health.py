from types import SimpleNamespace
import unittest

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.agent_self_metrics import normalize_agent_self_metrics


class HostWorker:
    def drain(self, limit):
        return ()

    def requeue_front(self, items):
        return len(tuple(items))

    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def snapshot(self):
        return SimpleNamespace(
            running=True,
            samples_collected=12,
            failed_samples=2,
            queued_samples=3,
            dropped_samples=4,
            last_error=None,
            last_stats=None,
        )


class SystemWorker:
    def drain_points(self, limit):
        return ()

    def requeue_points(self, items):
        return len(tuple(items))

    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def snapshot(self):
        return SimpleNamespace(
            running=True,
            collections=20,
            failed_collections=1,
            partial_collections=5,
            last_error=None,
            network_error=None,
            disk_error="disk sample failed",
            filesystem_error=None,
            process_error="process sample failed",
            buffer=SimpleNamespace(dropped_points=6),
        )


class AgentCollectorHealthTests(unittest.TestCase):
    def test_runtime_snapshot_carries_host_and_system_worker_health(self):
        runtime = LocalAgentRuntime(
            object(),
            host_worker=HostWorker(),
            system_worker=SystemWorker(),
        )
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot.host.samples_collected, 12)
        self.assertEqual(snapshot.host.dropped_samples, 4)
        self.assertEqual(snapshot.system.partial_collections, 5)
        self.assertEqual(snapshot.system.process_error, "process sample failed")

    def test_self_metrics_surface_collector_failures_and_queue_loss(self):
        runtime = LocalAgentRuntime(
            object(),
            host_worker=HostWorker(),
            system_worker=SystemWorker(),
        )
        points = normalize_agent_self_metrics(
            runtime.snapshot(),
            timestamp=100.0,
            hostname="node-a",
        )
        by_key = {(point.name, tuple(point.tags)): point.value for point in points}
        self.assertEqual(
            by_key[("container_profiler.host.dropped_samples", ("host:node-a",))],
            4.0,
        )
        self.assertEqual(
            by_key[
                (
                    "container_profiler.system.buffer_dropped_points",
                    ("host:node-a",),
                )
            ],
            6.0,
        )
        self.assertEqual(
            by_key[
                (
                    "container_profiler.system.check_error",
                    ("host:node-a", "check:disk"),
                )
            ],
            1.0,
        )
        self.assertEqual(
            by_key[
                (
                    "container_profiler.system.check_error",
                    ("host:node-a", "check:process"),
                )
            ],
            1.0,
        )
        self.assertEqual(
            by_key[
                (
                    "container_profiler.system.check_error",
                    ("host:node-a", "check:network"),
                )
            ],
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
