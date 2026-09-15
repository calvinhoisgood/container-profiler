from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.container_logs import ContainerLogWorker
from profiler.core.models import ContainerInfo
from profiler.core.workload import ContainerLogLine


def info(container_id="abc", status="running", name="web"):
    return ContainerInfo(
        id=container_id,
        name=name,
        status=status,
        image="demo:latest",
        created="",
        tags=("service:web", "env:test"),
    )


class FakeMonitor:
    def __init__(self, containers=(), error=None):
        self.containers = list(containers)
        self.error = error
        self.last_error = None
        self.client = object()

    def list_containers(self, all=False):
        self.last_error = self.error
        return [] if self.error else list(self.containers)


class FakeInspector:
    def __init__(self, batches):
        self.batches = {key: list(values) for key, values in batches.items()}
        self.last_error = None
        self.calls = []

    def get_logs(self, container_id, **kwargs):
        self.calls.append((container_id, kwargs))
        values = self.batches.get(container_id, [])
        if not values:
            return ()
        value = values.pop(0)
        if isinstance(value, Exception):
            self.last_error = str(value)
            return None
        self.last_error = None
        return tuple(value)


class ContainerLogWorkerTests(unittest.TestCase):
    def test_collects_identity_and_deduplicates_since_overlap(self):
        inspector = FakeInspector({
            "a": [
                [ContainerLogLine("2026-09-15T10:00:00Z", "one")],
                [
                    ContainerLogLine("2026-09-15T10:00:00Z", "one"),
                    ContainerLogLine("2026-09-15T10:00:01Z", "two"),
                ],
            ]
        })
        worker = ContainerLogWorker(
            monitor=FakeMonitor([info("a")]),
            inspector_factory=lambda client: inspector,
            wall_clock=lambda: 1_800_000_000.0,
        )
        self.assertTrue(worker.collect_once())
        self.assertTrue(worker.collect_once())
        records = worker.drain_records(10)
        self.assertEqual([record.message for record in records], ["one", "two"])
        self.assertTrue(all(record.container_id == "a" for record in records))
        self.assertTrue(all("service:web" in record.tags for record in records))
        self.assertEqual(worker.snapshot().duplicate_records, 1)
        self.assertLess(inspector.calls[1][1]["since"], 1_800_000_000)

    def test_identical_lines_in_one_snapshot_are_not_collapsed(self):
        repeated = ContainerLogLine("2026-09-15T10:00:00Z", "same")
        inspector = FakeInspector({"a": [[repeated, repeated], [repeated, repeated]]})
        worker = ContainerLogWorker(
            monitor=FakeMonitor([info("a")]),
            inspector_factory=lambda client: inspector,
        )
        self.assertTrue(worker.collect_once())
        first = worker.drain_records(10)
        self.assertEqual([record.message for record in first], ["same", "same"])
        self.assertTrue(worker.collect_once())
        self.assertEqual(worker.drain_records(10), ())
        self.assertEqual(worker.snapshot().duplicate_records, 2)

    def test_failure_isolated_and_container_and_buffer_bounds_visible(self):
        inspector = FakeInspector({
            "a": [[ContainerLogLine("2026-09-15T10:00:00Z", "a")]],
            "b": [RuntimeError("logs denied")],
            "c": [[ContainerLogLine("2026-09-15T10:00:02Z", "c")]],
        })
        worker = ContainerLogWorker(
            monitor=FakeMonitor([info("a"), info("b"), info("c"), info("old", "exited")]),
            inspector_factory=lambda client: inspector,
            max_containers=2,
            max_records=1,
            max_buffer_bytes=4096,
        )
        self.assertTrue(worker.collect_once())
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.containers_seen, 3)
        self.assertEqual(snapshot.containers_skipped, 1)
        self.assertEqual(snapshot.sample_failures, 1)
        self.assertIn("logs denied", snapshot.last_sample_errors[0])
        self.assertEqual(len(worker.drain_records(10)), 1)

    def test_discovery_failure_does_not_clear_existing_queue(self):
        monitor = FakeMonitor([info("a")])
        inspector = FakeInspector({"a": [[ContainerLogLine(None, "startup")]]})
        worker = ContainerLogWorker(monitor=monitor, inspector_factory=lambda client: inspector)
        self.assertTrue(worker.collect_once())
        monitor.error = "daemon unavailable"
        self.assertFalse(worker.collect_once())
        self.assertEqual(worker.snapshot().discovery_failures, 1)
        self.assertIn("daemon unavailable", worker.snapshot().last_docker_error)
        self.assertEqual(worker.drain_records(10)[0].message, "startup")

    def test_requeue_and_background_lifecycle_are_bounded(self):
        created = []
        inspector = FakeInspector({"a": [[ContainerLogLine(None, "line")]]})

        def monitor_factory():
            created.append(True)
            return FakeMonitor([info("a")])

        worker = ContainerLogWorker(
            monitor_factory=monitor_factory,
            inspector_factory=lambda client: inspector,
            interval_s=0.01,
        )
        self.assertEqual(created, [])
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and worker.snapshot().records_collected == 0:
            time.sleep(0.005)
        self.assertTrue(created)
        records = worker.drain_records(10)
        self.assertEqual(len(records), 1)
        self.assertEqual(worker.requeue_records(records), 1)
        self.assertEqual(worker.drain_records(10), records)
        self.assertTrue(worker.stop(timeout_s=1.0))
        self.assertFalse(worker.is_running())


if __name__ == "__main__":
    unittest.main()
