import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.alerting import AlertEngine, AlertStatus, ThresholdRule
from profiler.core.data_manager import DataManager
from profiler.core.models import ContainerStats, PowerStats


def stats(ts, cpu=10, memp=20, rx=0, tx=0, rxbps=None, txbps=None):
    return ContainerStats(ts, cpu, 100, 500, memp, rx, tx, rxbps, txbps, 3)


class DataManagerAnalyticsTests(unittest.TestCase):
    def test_summary_percentiles_sampling_and_energy(self):
        dm = DataManager()
        dm.start_recording("abc", 1000)
        dm.add_record("abc", stats(1000, cpu=10), PowerStats(cpu_power_w=100, gpu_power_w=50))
        dm.add_record("abc", stats(1001, cpu=20), PowerStats(cpu_power_w=100, gpu_power_w=50))
        dm.add_record("abc", stats(1003, cpu=30), PowerStats(cpu_power_w=100, gpu_power_w=50))
        summary = dm.build_summary()
        self.assertEqual(summary["session"]["sample_count"], 3)
        self.assertEqual(summary["session"]["duration_s"], 3.0)
        self.assertAlmostEqual(summary["metrics"]["cpu_percent"]["mean"], 20.0)
        self.assertAlmostEqual(summary["metrics"]["cpu_percent"]["p95"], 29.0)
        self.assertEqual(summary["sampling"]["estimated_missed_samples"], 1)
        self.assertAlmostEqual(summary["energy"]["cpu_wh"], 300 / 3600)
        self.assertAlmostEqual(summary["energy"]["gpu_wh"], 150 / 3600)
        self.assertAlmostEqual(summary["energy"]["total_wh"], 450 / 3600)
        self.assertAlmostEqual(summary["energy"]["cpu_coverage_ratio"], 1.0)

    def test_missing_power_is_not_zero_and_reduces_coverage(self):
        dm = DataManager()
        dm.start_recording("abc", 1000)
        dm.add_record("abc", stats(1), PowerStats(cpu_power_w=100))
        dm.add_record("abc", stats(2), PowerStats(cpu_power_w=None))
        dm.add_record("abc", stats(3), PowerStats(cpu_power_w=100))
        summary = dm.build_summary()
        self.assertEqual(summary["metrics"]["cpu_power_w"]["count"], 2)
        self.assertIsNone(summary["energy"]["cpu_wh"])
        self.assertEqual(summary["energy"]["cpu_coverage_ratio"], 0.0)

    def test_session_rejects_container_mix(self):
        dm = DataManager()
        dm.start_recording("a", 1000)
        with self.assertRaises(ValueError):
            dm.add_record("b", stats(1), PowerStats())

    def test_json_export(self):
        dm = DataManager()
        dm.start_recording("a", 1000)
        dm.add_record("a", stats(1), PowerStats())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "summary.json"
            self.assertTrue(dm.export_summary_json(path))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["session"]["container_id"], "a")


class AlertEngineTests(unittest.TestCase):
    def test_sustained_threshold_and_hysteresis_recovery(self):
        rule = ThresholdRule(
            "hot", "gpu_temp_c", "gte", 80,
            trigger_for_s=2, recovery_threshold=75,
        )
        engine = AlertEngine([rule])
        self.assertEqual(engine.evaluate(0, {"gpu_temp_c": 81}), [])
        self.assertEqual(engine.states()["hot"], AlertStatus.PENDING)
        self.assertEqual(engine.evaluate(1, {"gpu_temp_c": 82}), [])
        events = engine.evaluate(2, {"gpu_temp_c": 83})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].current, AlertStatus.FIRING)
        self.assertEqual(engine.evaluate(3, {"gpu_temp_c": 79}), [])
        events = engine.evaluate(4, {"gpu_temp_c": 74})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].current, AlertStatus.OK)

    def test_missing_value_does_not_recover_firing_alert(self):
        engine = AlertEngine([ThresholdRule("cpu", "cpu_percent", "gt", 90)])
        self.assertEqual(
            engine.evaluate(0, {"cpu_percent": 99})[0].current,
            AlertStatus.FIRING,
        )
        self.assertEqual(engine.evaluate(1, {"cpu_percent": None}), [])
        self.assertEqual(engine.states()["cpu"], AlertStatus.FIRING)


if __name__ == "__main__":
    unittest.main()
