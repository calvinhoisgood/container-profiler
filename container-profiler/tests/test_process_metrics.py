import unittest

from profiler.core.host_io import HostIOSnapshot
from profiler.core.host_process import ProcessSummary
from profiler.core.system_metrics import (
    NativeSystemMetricsCollector,
    SystemMetricsRuntimeWorker,
    normalize_process_metrics,
)


class Backend:
    def read(self):
        return HostIOSnapshot(1.0, (), ())


class ProcessMonitor:
    def __init__(self, stats=None, *, error=None):
        self.stats = stats
        self.last_error = error
        self.calls = 0

    def get_stats(self):
        self.calls += 1
        return self.stats


class ProcessMetricsTests(unittest.TestCase):
    def test_normalized_summary_is_low_cardinality(self):
        points = normalize_process_metrics(
            ProcessSummary(
                5.0,
                10,
                23,
                running=2,
                sleeping=7,
                blocked=1,
                stopped=0,
                zombies=0,
                unknown=0,
                skipped_processes=1,
            ),
            hostname="host-a",
        )
        values = {(point.name, tuple(point.tags)): point.value for point in points}
        self.assertEqual(
            values[("container_profiler.host.processes", ("host:host-a",))],
            10.0,
        )
        self.assertEqual(
            values[("container_profiler.host.threads", ("host:host-a",))],
            23.0,
        )
        self.assertEqual(
            values[
                (
                    "container_profiler.host.process_state",
                    ("host:host-a", "state:running"),
                )
            ],
            2.0,
        )
        self.assertTrue(
            all(
                point.target_key == "host-process" and point.source == "system"
                for point in points
            )
        )

    def test_process_collection_has_independent_cadence_without_catchup(self):
        now = [0.0]
        monitor = ProcessMonitor(
            ProcessSummary(
                1.0,
                2,
                3,
                running=1,
                sleeping=1,
                blocked=0,
                stopped=0,
                zombies=0,
                unknown=0,
            )
        )
        collector = NativeSystemMetricsCollector(
            Backend(),
            process_monitor=monitor,
            process_interval_s=10.0,
            monotonic_clock=lambda: now[0],
        )
        self.assertIn(
            "container_profiler.host.processes",
            {point.name for point in collector.collect()},
        )
        now[0] = 9.9
        self.assertNotIn(
            "container_profiler.host.processes",
            {point.name for point in collector.collect()},
        )
        now[0] = 10.0
        self.assertIn(
            "container_profiler.host.processes",
            {point.name for point in collector.collect()},
        )
        self.assertEqual(monitor.calls, 2)

    def test_process_failure_is_partial_not_total_collection_failure(self):
        monitor = ProcessMonitor(None, error="process snapshot denied")
        collector = NativeSystemMetricsCollector(
            Backend(),
            process_monitor=monitor,
            monotonic_clock=lambda: 0.0,
        )
        worker = SystemMetricsRuntimeWorker(collector)
        self.assertEqual(worker.collect_once(), 0)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.collections, 1)
        self.assertEqual(snapshot.failed_collections, 0)
        self.assertEqual(snapshot.partial_collections, 1)
        self.assertEqual(snapshot.process_error, "process snapshot denied")


if __name__ == "__main__":
    unittest.main()
