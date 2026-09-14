import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.alert_runtime import AlertRuntime


class AlertRuntimeTests(unittest.TestCase):
    @staticmethod
    def sample(cpu: float):
        return SimpleNamespace(cpu_percent=cpu), SimpleNamespace()

    def test_missing_file_installs_empty_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AlertRuntime(Path(directory) / "alerts.json")
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.rule_count, 0)
            self.assertEqual(snapshot.generation, 1)
            self.assertIsNone(snapshot.last_config_error)

    def test_hot_reload_and_evaluate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            path.write_text(json.dumps({
                "rules": [{
                    "name": "cpu-hot",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 90,
                }]
            }), encoding="utf-8")
            runtime = AlertRuntime(path)
            stats, power = self.sample(99)
            events = runtime.evaluate(1.0, stats, power)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].rule_name, "cpu-hot")

    def test_invalid_reload_keeps_last_known_good_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            path.write_text(json.dumps({
                "rules": [{
                    "name": "cpu-hot",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 90,
                }]
            }), encoding="utf-8")
            runtime = AlertRuntime(path)
            generation = runtime.generation

            path.write_text("{not-json", encoding="utf-8")
            self.assertFalse(runtime.reload(force=True))
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.generation, generation)
            self.assertEqual(snapshot.rule_count, 1)
            self.assertIsNotNone(snapshot.last_config_error)

            stats, power = self.sample(99)
            self.assertEqual(len(runtime.evaluate(2.0, stats, power)), 1)

    def test_event_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            path.write_text(json.dumps({
                "rules": [{
                    "name": "cpu-hot",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 90,
                }]
            }), encoding="utf-8")
            runtime = AlertRuntime(path, max_events=2)
            for timestamp, value in enumerate((99, 0, 99, 0)):
                stats, power = self.sample(value)
                runtime.evaluate(float(timestamp), stats, power)
            self.assertEqual(len(runtime.events), 2)
            self.assertEqual(runtime.snapshot().event_count, 2)

    def test_unchanged_file_does_not_reset_engine_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            path.write_text(json.dumps({
                "rules": [{
                    "name": "cpu-hot",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 90,
                    "trigger_for_s": 10,
                }]
            }), encoding="utf-8")
            runtime = AlertRuntime(path)
            stats, power = self.sample(99)
            runtime.evaluate(0.0, stats, power)
            before = runtime.snapshot()
            self.assertFalse(runtime.reload_if_changed())
            after = runtime.snapshot()
            self.assertEqual(before.generation, after.generation)
            self.assertEqual(before.states, after.states)


if __name__ == "__main__":
    unittest.main()
