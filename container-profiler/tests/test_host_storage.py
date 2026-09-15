import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.host_monitor import HostStats
from profiler.core.storage import SQLiteTelemetryStore


def host_sample(timestamp: float, cpu: float | None = 50.0) -> HostStats:
    return HostStats(
        timestamp=timestamp,
        cpu_percent=cpu,
        logical_cpus=8,
        memory_total_mb=16_384.0,
        memory_available_mb=8192.0,
        memory_used_mb=8192.0,
        memory_percent=50.0,
        uptime_s=1000.0 + timestamp,
        load_1=1.0,
        load_5=2.0,
        load_15=3.0,
    )


class HostStorageTests(unittest.TestCase):
    def test_round_trip_preserves_unknown_cpu_and_time_filters(self):
        with SQLiteTelemetryStore() as store:
            self.assertEqual(store.append_host_samples((
                host_sample(10.0, None),
                host_sample(20.0, 25.0),
                host_sample(30.0, 75.0),
            )), 3)
            rows = store.query_host_samples(start_timestamp=15.0, end_timestamp=30.0)
            self.assertEqual([row["timestamp"] for row in rows], [30.0, 20.0])
            self.assertEqual(rows[0]["cpu_percent"], 75.0)
            all_rows = store.query_host_samples(limit=10)
            self.assertIsNone(all_rows[-1]["cpu_percent"])
            self.assertEqual(all_rows[0]["logical_cpus"], 8)

    def test_retention_prunes_age_then_oldest_rows(self):
        with SQLiteTelemetryStore() as store:
            store.append_host_samples(host_sample(float(i)) for i in range(10))
            self.assertEqual(store.prune_host_samples(before_timestamp=3.0), 3)
            self.assertEqual(store.prune_host_samples(max_rows=4), 3)
            self.assertEqual(
                [row["timestamp"] for row in store.query_host_samples(limit=20)],
                [9.0, 8.0, 7.0, 6.0],
            )
            with self.assertRaises(ValueError):
                store.prune_host_samples(max_rows=-1)


if __name__ == "__main__":
    unittest.main()
