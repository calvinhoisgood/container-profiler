import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from profiler.core.self_telemetry import AgentSelfTelemetry, advance_fixed_rate_deadline


class AgentSelfTelemetryTests(unittest.TestCase):
    def test_snapshot_tracks_latency_lag_failures_and_queue_depth(self):
        health = AgentSelfTelemetry(interval_s=1.0, window_size=8)
        health.record_cycle(
            scheduled_at=10.0,
            started_at=10.1,
            finished_at=10.4,
            success=True,
            queue_depth=2,
        )
        health.record_cycle(
            scheduled_at=11.0,
            started_at=11.2,
            finished_at=12.3,
            success=False,
            queue_depth=5,
        )
        health.record_skipped_ticks(1)
        snapshot = health.snapshot()

        self.assertEqual(snapshot.cycles, 2)
        self.assertEqual(snapshot.successful_cycles, 1)
        self.assertEqual(snapshot.failed_cycles, 1)
        self.assertEqual(snapshot.skipped_ticks, 1)
        self.assertEqual(snapshot.overrun_cycles, 1)
        self.assertAlmostEqual(snapshot.success_ratio, 0.5)
        self.assertAlmostEqual(snapshot.overrun_ratio, 0.5)
        self.assertAlmostEqual(snapshot.collection_latency_ms_mean, 700.0)
        self.assertAlmostEqual(snapshot.scheduling_lag_ms_mean, 150.0)
        self.assertEqual(snapshot.last_queue_depth, 5)
        self.assertEqual(snapshot.max_queue_depth, 5)

    def test_rolling_window_limits_distribution_memory_not_totals(self):
        health = AgentSelfTelemetry(interval_s=1.0, window_size=2)
        for i, latency in enumerate((0.1, 0.2, 0.3)):
            health.record_cycle(
                scheduled_at=float(i),
                started_at=float(i),
                finished_at=float(i) + latency,
                success=True,
            )
        snapshot = health.snapshot()
        self.assertEqual(snapshot.cycles, 3)
        self.assertAlmostEqual(snapshot.collection_latency_ms_mean, 250.0)
        self.assertAlmostEqual(snapshot.collection_latency_ms_max, 300.0)

    def test_fixed_rate_deadline_skips_missed_ticks(self):
        self.assertEqual(advance_fixed_rate_deadline(10.0, 10.4, 1.0), (11.0, 0))
        self.assertEqual(advance_fixed_rate_deadline(10.0, 12.2, 1.0), (13.0, 2))

    def test_invalid_values_fail_fast(self):
        with self.assertRaises(ValueError):
            AgentSelfTelemetry(interval_s=0)
        health = AgentSelfTelemetry(interval_s=1.0)
        with self.assertRaises(ValueError):
            health.record_cycle(
                scheduled_at=0,
                started_at=2,
                finished_at=1,
                success=True,
            )
        with self.assertRaises(ValueError):
            health.record_skipped_ticks(-1)


if __name__ == "__main__":
    unittest.main()
