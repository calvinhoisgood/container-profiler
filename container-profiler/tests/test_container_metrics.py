from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.container_metrics import ContainerMetricsWorker, normalize_container_stats
from profiler.core.models import ContainerInfo, ContainerStats


def info(container_id="abc", status="running"):
    return ContainerInfo(
        id=container_id,
        name="web",
        status=status,
        image="demo:latest",
        created="",
        tags=("service:web", "env:test"),
    )


def stats(timestamp=10.0):
    return ContainerStats(
        timestamp=timestamp,
        cpu_percent=25.5,
        memory_mb=128.0,
        memory_limit_mb=512.0,
        memory_percent=25.0,
        network_rx_bytes=1000,
        network_tx_bytes=2000,
        network_rx_bps=100.0,
        network_tx_bps=200.0,
        pids=7,
    )


class FakeMonitor:
    def __init__(self, containers=(), values=None, list_error=None):
        self.containers = list(containers)
        self.values = dict(values or {})
        self.list_error = list_error
        self.last_error = None
        self.list_calls = 0

    def list_containers(self, all=False):
        self.list_calls += 1
        self.last_error = self.list_error
        return list(self.containers) if not self.list_error else []

    def get_stats(self, container_id):
        value = self.values.get(container_id)
        if isinstance(value, Exception):
            self.last_error = str(value)
            return None
        self.last_error = None
        return value


class ContainerMetricsTests(unittest.TestCase):
    def test_normalizes_native_docker_stats_with_identity(self):
        points = normalize_container_stats(info(), stats())
        by_name = {point.name: point for point in points}
        self.assertEqual(len(points), 9)
        self.assertEqual(by_name["container.cpu.usage_percent"].value, 25.5)
        self.assertEqual(by_name["container.memory.usage_mib"].unit, "MiB")
        self.assertEqual(by_name["container.network.rx_bytes_per_second"].value, 100.0)
        self.assertEqual(by_name["container.pids"].value, 7.0)
        self.assertTrue(all(point.source == "docker" for point in points))
        self.assertTrue(all(point.container_id == "abc" for point in points))
        self.assertTrue(all("service:web" in point.tags for point in points))

    def test_collect_once_isolates_container_failure_and_bounds_container_count(self):
        containers = [info("a"), info("b"), info("c"), info("old", "exited")]
        monitor = FakeMonitor(
            containers,
            values={"a": stats(), "b": RuntimeError("stats denied"), "c": stats()},
        )
        worker = ContainerMetricsWorker(monitor=monitor, max_containers=2)
        self.assertTrue(worker.collect_once())
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.containers_seen, 3)
        self.assertEqual(snapshot.containers_sampled, 1)
        self.assertEqual(snapshot.containers_skipped, 1)
        self.assertEqual(snapshot.sample_failures, 1)
        self.assertIn("stats denied", snapshot.last_sample_errors[0])
        points = worker.drain_points(100)
        self.assertEqual(len(points), 9)
        self.assertTrue(all(point.container_id == "a" for point in points))

    def test_discovery_failure_keeps_worker_alive_and_is_visible(self):
        monitor = FakeMonitor(list_error="daemon unavailable")
        worker = ContainerMetricsWorker(monitor=monitor)
        self.assertFalse(worker.collect_once())
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.discovery_failures, 1)
        self.assertIn("daemon unavailable", snapshot.last_docker_error)
        self.assertEqual(worker.drain_points(), ())

    def test_monitor_creation_is_lazy_and_background_thread_stops(self):
        created = []

        def factory():
            created.append(True)
            return FakeMonitor([info()], {"abc": stats()})

        worker = ContainerMetricsWorker(
            monitor_factory=factory,
            interval_s=0.01,
        )
        self.assertEqual(created, [])
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and worker.snapshot().containers_sampled == 0:
            time.sleep(0.005)
        self.assertTrue(created)
        self.assertGreater(worker.snapshot().containers_sampled, 0)
        self.assertTrue(worker.stop(timeout_s=1.0))
        self.assertFalse(worker.is_running())


if __name__ == "__main__":
    unittest.main()
