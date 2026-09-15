import unittest

from profiler.core.host_filesystem import FilesystemStats
from profiler.core.host_io import DiskCounters, HostIOSnapshot, NetworkCounters
from profiler.core.system_metrics import NativeSystemMetricsCollector, SystemMetricsRuntimeWorker


class SequenceBackend:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def read(self):
        if not self.snapshots:
            raise OSError("no more snapshots")
        return self.snapshots.pop(0)


class FakeFilesystemMonitor:
    def __init__(self, stats=(), *, error=None):
        self.stats = tuple(stats)
        self.last_error = error
        self.calls = 0

    def get_stats(self):
        self.calls += 1
        return self.stats


def net(rx, tx, *, packets_in=10, packets_out=20, errors_in=1, errors_out=2, drops_in=3, drops_out=4):
    return NetworkCounters(
        "Ethernet",
        rx,
        tx,
        packets_in,
        packets_out,
        errors_in,
        errors_out,
        drops_in,
        drops_out,
    )


def disk(read_bytes, write_bytes, *, reads=10, writes=20):
    return DiskCounters("PhysicalDrive0", reads, writes, read_bytes, write_bytes, 0)


def filesystem():
    return FilesystemStats(
        timestamp=5.0,
        mountpoint="C:\\",
        device="C:\\",
        filesystem="ntfs",
        total_bytes=1000,
        used_bytes=250,
        free_bytes=750,
        used_percent=25.0,
    )


class NativeSystemMetricsTests(unittest.TestCase):
    def test_first_observation_emits_raw_network_gauges_but_not_rates(self):
        backend = SequenceBackend([
            HostIOSnapshot(10.0, (net(100, 200),), (disk(1024, 2048),)),
        ])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host-a")
        points = collector.collect()
        names = {point.name for point in points}
        self.assertIn("system.net.packets_in.count", names)
        self.assertIn("system.net.packets_out.drop", names)
        self.assertNotIn("system.net.bytes_rcvd", names)
        self.assertNotIn("system.net.bytes_sent", names)
        self.assertNotIn("system.io.r_s", names)
        self.assertTrue(all("host:host-a" in point.tags for point in points))
        self.assertTrue(all(point.source == "system" for point in points))

    def test_second_observation_emits_datadog_style_rates_and_device_tags(self):
        backend = SequenceBackend([
            HostIOSnapshot(10.0, (net(100, 200),), (disk(1024, 2048),)),
            HostIOSnapshot(
                12.0,
                (net(500, 800, packets_in=14, packets_out=26),),
                (disk(5120, 10240, reads=14, writes=26),),
            ),
        ])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host-a")
        collector.collect()
        points = collector.collect()
        values = {point.name: point.value for point in points}
        self.assertEqual(values["system.net.bytes_rcvd"], 200.0)
        self.assertEqual(values["system.net.bytes_sent"], 300.0)
        self.assertEqual(values["system.io.r_s"], 2.0)
        self.assertEqual(values["system.io.w_s"], 3.0)
        self.assertEqual(values["system.io.rkb_s"], 2.0)
        self.assertEqual(values["system.io.wkb_s"], 4.0)
        net_point = next(p for p in points if p.name == "system.net.bytes_rcvd")
        disk_point = next(p for p in points if p.name == "system.io.r_s")
        self.assertIn("device:Ethernet", net_point.tags)
        self.assertIn("device:PhysicalDrive0", disk_point.tags)

    def test_counter_reset_suppresses_rate_metrics_for_only_that_interval(self):
        backend = SequenceBackend([
            HostIOSnapshot(1.0, (net(1000, 2000),), ()),
            HostIOSnapshot(2.0, (net(10, 20),), ()),
            HostIOSnapshot(3.0, (net(110, 220, packets_in=11, packets_out=21),), ()),
        ])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host")
        collector.collect()
        reset_points = collector.collect()
        self.assertNotIn("system.net.bytes_rcvd", {p.name for p in reset_points})
        recovered = collector.collect()
        values = {p.name: p.value for p in recovered}
        self.assertEqual(values["system.net.bytes_rcvd"], 100.0)
        self.assertEqual(values["system.net.bytes_sent"], 200.0)

    def test_filesystem_metrics_use_disk_namespace_and_fraction_semantics(self):
        monitor = FakeFilesystemMonitor([filesystem()])
        backend = SequenceBackend([HostIOSnapshot(5.0, (), ())])
        collector = NativeSystemMetricsCollector(
            backend,
            filesystem_monitor=monitor,
            hostname=lambda: "host-a",
            monotonic_clock=lambda: 0.0,
        )
        points = collector.collect()
        values = {point.name: point.value for point in points}
        self.assertEqual(values["system.disk.total"], 1000.0)
        self.assertEqual(values["system.disk.free"], 750.0)
        self.assertEqual(values["system.disk.used"], 250.0)
        self.assertEqual(values["system.disk.in_use"], 0.25)
        self.assertEqual(values["system.disk.utilized"], 25.0)
        point = next(p for p in points if p.name == "system.disk.total")
        self.assertIn("device:C:\\", point.tags)
        self.assertIn("mountpoint:C:\\", point.tags)
        self.assertIn("filesystem:ntfs", point.tags)

    def test_filesystem_check_has_independent_cadence_without_catchup(self):
        now = [0.0]
        monitor = FakeFilesystemMonitor([filesystem()])
        backend = SequenceBackend([
            HostIOSnapshot(1.0, (), ()),
            HostIOSnapshot(2.0, (), ()),
            HostIOSnapshot(3.0, (), ()),
        ])
        collector = NativeSystemMetricsCollector(
            backend,
            filesystem_monitor=monitor,
            filesystem_interval_s=15.0,
            monotonic_clock=lambda: now[0],
        )
        first = collector.collect()
        self.assertIn("system.disk.total", {p.name for p in first})
        now[0] = 14.9
        second = collector.collect()
        self.assertNotIn("system.disk.total", {p.name for p in second})
        now[0] = 30.0
        third = collector.collect()
        self.assertIn("system.disk.total", {p.name for p in third})
        self.assertEqual(monitor.calls, 2)

    def test_partial_backend_failure_keeps_healthy_side_and_health_state(self):
        backend = SequenceBackend([
            HostIOSnapshot(
                1.0,
                (net(1, 2),),
                (),
                disk_error="disk access denied",
            ),
        ])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host")
        worker = SystemMetricsRuntimeWorker(collector, max_points=100, max_bytes=100_000)
        accepted = worker.collect_once()
        self.assertGreater(accepted, 0)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.collections, 1)
        self.assertEqual(snapshot.failed_collections, 0)
        self.assertEqual(snapshot.partial_collections, 1)
        self.assertEqual(snapshot.disk_error, "disk access denied")
        self.assertIsNone(snapshot.network_error)

    def test_filesystem_failure_is_visible_but_does_not_drop_network_metrics(self):
        monitor = FakeFilesystemMonitor((), error="volume query failed")
        backend = SequenceBackend([HostIOSnapshot(1.0, (net(1, 2),), ())])
        collector = NativeSystemMetricsCollector(
            backend,
            filesystem_monitor=monitor,
            monotonic_clock=lambda: 0.0,
        )
        worker = SystemMetricsRuntimeWorker(collector, max_points=100, max_bytes=100_000)
        self.assertGreater(worker.collect_once(), 0)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.partial_collections, 1)
        self.assertEqual(snapshot.filesystem_error, "volume query failed")
        self.assertEqual(snapshot.failed_collections, 0)

    def test_total_collection_failure_does_not_destroy_buffered_points(self):
        backend = SequenceBackend([
            HostIOSnapshot(1.0, (net(1, 2),), ()),
        ])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host")
        worker = SystemMetricsRuntimeWorker(collector, max_points=100, max_bytes=100_000)
        first = worker.collect_once()
        self.assertGreater(first, 0)
        self.assertEqual(worker.collect_once(), 0)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.failed_collections, 1)
        self.assertGreater(snapshot.buffer.queued_points, 0)
        self.assertIn("no more snapshots", snapshot.last_error)

    def test_worker_buffer_is_bounded_under_sustained_production(self):
        snapshots = [
            HostIOSnapshot(float(i), (net(i * 100, i * 200),), ())
            for i in range(1, 20)
        ]
        collector = NativeSystemMetricsCollector(SequenceBackend(snapshots), hostname=lambda: "host")
        worker = SystemMetricsRuntimeWorker(collector, max_points=10, max_bytes=100_000)
        for _ in range(19):
            worker.collect_once()
        snapshot = worker.snapshot()
        self.assertLessEqual(snapshot.buffer.queued_points, 10)
        self.assertGreater(snapshot.buffer.dropped_points, 0)

    def test_requeue_preserves_points_after_storage_failure(self):
        backend = SequenceBackend([HostIOSnapshot(1.0, (net(1, 2),), ())])
        collector = NativeSystemMetricsCollector(backend, hostname=lambda: "host")
        worker = SystemMetricsRuntimeWorker(collector, max_points=100, max_bytes=100_000)
        worker.collect_once()
        points = worker.drain_points(100)
        self.assertGreater(len(points), 0)
        self.assertEqual(worker.snapshot().buffer.queued_points, 0)
        restored = worker.requeue_points(points)
        self.assertEqual(restored, len(points))
        self.assertEqual(worker.snapshot().buffer.queued_points, len(points))


if __name__ == "__main__":
    unittest.main()
