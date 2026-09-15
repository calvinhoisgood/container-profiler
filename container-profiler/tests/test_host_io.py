import unittest

from profiler.core.host_io import (
    DiskCounters,
    HostIORateTracker,
    LinuxProcIOBackend,
    NetworkCounters,
)


class LinuxProcIOTests(unittest.TestCase):
    def test_parse_network_counters_and_bound(self):
        text = """Inter-| Receive | Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
  eth0: 1000 10 1 2 0 0 0 0 2000 20 3 4 0 0 0 0
    lo: 500 5 0 0 0 0 0 0 500 5 0 0 0 0 0 0
"""
        rows = LinuxProcIOBackend.parse_net_dev(text, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].interface, "eth0")
        self.assertEqual((rows[0].rx_bytes, rows[0].tx_bytes), (1000, 2000))
        self.assertEqual((rows[0].rx_errors, rows[0].tx_dropped), (1, 4))

    def test_network_parser_skips_malformed_or_negative_rows(self):
        text = """bad: nope
neg0: -1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0
ok0: 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0
"""
        self.assertEqual([x.interface for x in LinuxProcIOBackend.parse_net_dev(text)], ["ok0"])

    def test_parse_diskstats_uses_linux_512_byte_sector_units(self):
        text = "8 0 sda 10 0 20 0 30 0 40 0 0 50 0 0 0 0 0\n"
        row = LinuxProcIOBackend.parse_diskstats(text)[0]
        self.assertEqual(row.device, "sda")
        self.assertEqual(row.read_bytes, 20 * 512)
        self.assertEqual(row.write_bytes, 40 * 512)
        self.assertEqual(row.io_time_ms, 50)

    def test_backend_reads_both_proc_sources(self):
        data = {
            "/proc/net/dev": "eth0: 1 1 0 0 0 0 0 0 2 2 0 0 0 0 0 0\n",
            "/proc/diskstats": "8 0 sda 1 0 2 0 3 0 4 0 0 5 0\n",
        }
        backend = LinuxProcIOBackend(reader=data.__getitem__, wall_clock=lambda: 12.5)
        snapshot = backend.read()
        self.assertEqual(snapshot.timestamp, 12.5)
        self.assertEqual(snapshot.network[0].interface, "eth0")
        self.assertEqual(snapshot.disks[0].device, "sda")


class RateTrackerTests(unittest.TestCase):
    @staticmethod
    def net(rx, tx, rp=10, tp=20):
        return NetworkCounters("eth0", rx, tx, rp, tp, 0, 0, 0, 0)

    @staticmethod
    def disk(rb, wb, ro=10, wo=20):
        return DiskCounters("sda", ro, wo, rb, wb, 0)

    def test_first_sample_is_unknown_then_rates_are_derived(self):
        tracker = HostIORateTracker()
        first = tracker.update_network(10.0, [self.net(100, 200)])[0]
        self.assertIsNone(first.read_bps)
        second = tracker.update_network(12.0, [self.net(300, 500, 14, 26)])[0]
        self.assertEqual(second.read_bps, 100.0)
        self.assertEqual(second.write_bps, 150.0)
        self.assertEqual(second.read_ops_s, 2.0)
        self.assertEqual(second.write_ops_s, 3.0)

    def test_counter_reset_never_emits_negative_rate(self):
        tracker = HostIORateTracker()
        tracker.update_network(10.0, [self.net(1000, 2000)])
        reset = tracker.update_network(11.0, [self.net(10, 20)])[0]
        self.assertTrue(reset.reset)
        self.assertIsNone(reset.read_bps)
        recovered = tracker.update_network(12.0, [self.net(110, 220, 11, 21)])[0]
        self.assertEqual(recovered.read_bps, 100.0)

    def test_long_gap_is_baseline_reset_not_catchup_average(self):
        tracker = HostIORateTracker(max_interval_s=5)
        tracker.update_disks(0.0, [self.disk(100, 200)])
        rate = tracker.update_disks(10.0, [self.disk(1000, 2000)])[0]
        self.assertTrue(rate.reset)
        self.assertIsNone(rate.write_bps)

    def test_removed_device_drops_baseline(self):
        tracker = HostIORateTracker()
        tracker.update_disks(1.0, [self.disk(100, 100)])
        tracker.update_disks(2.0, [])
        again = tracker.update_disks(3.0, [self.disk(200, 200)])[0]
        self.assertIsNone(again.read_bps)
        self.assertFalse(again.reset)


if __name__ == "__main__":
    unittest.main()
