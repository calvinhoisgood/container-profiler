import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.alert_config import (
    AlertConfigError,
    load_alert_rules,
    parse_alert_config,
    sample_metrics,
)


class AlertConfigTests(unittest.TestCase):
    def test_parses_sustained_critical_rule_with_hysteresis(self):
        rules = parse_alert_config({
            "schema_version": 1,
            "rules": [{
                "name": "gpu-hot",
                "metric": "gpu_temp_c",
                "operator": "gte",
                "threshold": 80,
                "recovery_threshold": 75,
                "trigger_for_s": 5,
                "severity": "critical",
            }],
        })
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, "gpu-hot")
        self.assertEqual(rules[0].trigger_for_s, 5.0)
        self.assertEqual(rules[0].recovery_threshold, 75.0)

    def test_disabled_rule_is_omitted(self):
        rules = parse_alert_config({
            "rules": [{
                "name": "disabled",
                "metric": "cpu_percent",
                "operator": "gt",
                "threshold": 90,
                "enabled": False,
            }]
        })
        self.assertEqual(rules, ())

    def test_high_threshold_recovery_cannot_be_above_trigger(self):
        with self.assertRaises(AlertConfigError):
            parse_alert_config({
                "rules": [{
                    "name": "cpu",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 90,
                    "recovery_threshold": 95,
                }]
            })

    def test_low_threshold_recovery_cannot_be_below_trigger(self):
        with self.assertRaises(AlertConfigError):
            parse_alert_config({
                "rules": [{
                    "name": "low-power",
                    "metric": "gpu_power_w",
                    "operator": "lt",
                    "threshold": 10,
                    "recovery_threshold": 5,
                }]
            })

    def test_nonfinite_threshold_is_rejected(self):
        with self.assertRaises(AlertConfigError):
            parse_alert_config({
                "rules": [{
                    "name": "bad",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": float("nan"),
                }]
            })

    def test_unknown_metric_and_field_are_rejected(self):
        with self.assertRaises(AlertConfigError):
            parse_alert_config({
                "rules": [{
                    "name": "bad-metric",
                    "metric": "not_a_metric",
                    "operator": "gt",
                    "threshold": 1,
                }]
            })
        with self.assertRaises(AlertConfigError):
            parse_alert_config({
                "rules": [{
                    "name": "typo",
                    "metric": "cpu_percent",
                    "operator": "gt",
                    "threshold": 1,
                    "threshhold": 2,
                }]
            })

    def test_duplicate_enabled_names_are_rejected(self):
        rule = {
            "name": "same",
            "metric": "cpu_percent",
            "operator": "gt",
            "threshold": 90,
        }
        with self.assertRaises(AlertConfigError):
            parse_alert_config({"rules": [rule, dict(rule)]})

    def test_missing_file_disables_alerts_and_json_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            self.assertEqual(load_alert_rules(path), ())
            path.write_text(json.dumps({"rules": []}), encoding="utf-8")
            self.assertEqual(load_alert_rules(path), ())

    def test_sample_metrics_combines_resource_and_power_samples(self):
        stats = SimpleNamespace(cpu_percent=12.5, memory_percent=30.0)
        power = SimpleNamespace(gpu_temp_c=70.0, gpu_power_w=120.0)
        metrics = sample_metrics(stats, power)
        self.assertEqual(metrics["cpu_percent"], 12.5)
        self.assertEqual(metrics["gpu_temp_c"], 70.0)
        self.assertEqual(metrics["gpu_power_w"], 120.0)
        self.assertIsNone(metrics["network_rx_bps"])


if __name__ == "__main__":
    unittest.main()
