import json
import tempfile
import unittest
from pathlib import Path

from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.metric_alerts import (
    MetricAlertConfigError,
    MetricAlertRuntime,
    parse_metric_alert_config,
)


def point(timestamp, value, *, target="target-a", tags=(), container=None):
    return CustomMetricPoint(
        timestamp=float(timestamp),
        name="my.metric",
        value=float(value),
        tags=tuple(tags),
        metric_type="gauge",
        source="test",
        target_key=target,
        container_id=container,
    )


class MetricAlertConfigTests(unittest.TestCase):
    def test_accepts_arbitrary_metric_and_hysteresis(self):
        rules = parse_metric_alert_config(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "name": "hot",
                        "metric": "my.metric",
                        "operator": "gt",
                        "threshold": 10,
                        "recovery_threshold": 8,
                    }
                ],
            }
        )
        self.assertEqual(rules[0].metric, "my.metric")
        self.assertEqual(rules[0].recovery_threshold, 8.0)

    def test_bad_hysteresis_and_duplicate_names_are_rejected(self):
        with self.assertRaises(MetricAlertConfigError):
            parse_metric_alert_config(
                {
                    "rules": [
                        {
                            "name": "hot",
                            "metric": "x",
                            "operator": "gt",
                            "threshold": 10,
                            "recovery_threshold": 11,
                        }
                    ]
                }
            )
        duplicate = {
            "name": "same",
            "metric": "x",
            "operator": "gt",
            "threshold": 1,
        }
        with self.assertRaises(MetricAlertConfigError):
            parse_metric_alert_config({"rules": [duplicate, duplicate]})


class MetricAlertRuntimeTests(unittest.TestCase):
    def _runtime(self, directory, *, max_series=5000, trigger_for_s=0):
        path = Path(directory) / "alerts.json"
        path.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "name": "hot",
                            "metric": "my.metric",
                            "operator": "gt",
                            "threshold": 10,
                            "recovery_threshold": 8,
                            "trigger_for_s": trigger_for_s,
                            "severity": "critical",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path, MetricAlertRuntime(path, max_series=max_series)

    def test_series_are_isolated_by_target_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            _, runtime = self._runtime(directory)
            transitions = runtime.evaluate_points(
                [
                    point(1, 11, target="a", tags=("zone:a",)),
                    point(1, 5, target="b", tags=("zone:b",)),
                ]
            )
            self.assertEqual(len(transitions), 1)
            self.assertEqual(transitions[0].target_key, "a")
            self.assertEqual(transitions[0].event.current.value, "firing")

            recovery = runtime.evaluate_points(
                [point(2, 8, target="a", tags=("zone:a",))]
            )
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].event.current.value, "ok")
            self.assertEqual(runtime.snapshot().tracked_series, 2)

    def test_sustained_threshold_state_is_per_series(self):
        with tempfile.TemporaryDirectory() as directory:
            _, runtime = self._runtime(directory, trigger_for_s=5)
            self.assertEqual(
                runtime.evaluate_points([point(1, 11, target="a")]),
                (),
            )
            # A different target must not inherit target-a's pending clock.
            self.assertEqual(
                runtime.evaluate_points([point(10, 11, target="b")]),
                (),
            )
            firing = runtime.evaluate_points([point(11, 11, target="a")])
            self.assertEqual(len(firing), 1)
            self.assertEqual(firing[0].target_key, "a")

    def test_series_lru_is_bounded_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            _, runtime = self._runtime(directory, max_series=2)
            for index in range(3):
                runtime.evaluate_points(
                    [point(index + 1, 0, target=f"target-{index}")]
                )
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.tracked_series, 2)
            self.assertEqual(snapshot.evicted_series, 1)

    def test_invalid_reload_keeps_last_known_good_rules_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path, runtime = self._runtime(directory)
            runtime.evaluate_points([point(1, 11)])
            self.assertEqual(runtime.snapshot().tracked_series, 1)

            path.write_text("{bad", encoding="utf-8")
            self.assertFalse(runtime.reload(force=True))
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.rule_count, 1)
            self.assertEqual(snapshot.tracked_series, 1)
            self.assertIsNotNone(snapshot.last_config_error)
            recovery = runtime.evaluate_points([point(2, 8)])
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0].event.current.value, "ok")

    def test_valid_reload_resets_old_rule_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path, runtime = self._runtime(directory)
            runtime.evaluate_points([point(1, 11)])
            path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "name": "hot",
                                "metric": "my.metric",
                                "operator": "gt",
                                "threshold": 20,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(runtime.reload(force=True))
            self.assertEqual(runtime.snapshot().tracked_series, 0)
            self.assertEqual(runtime.evaluate_points([point(2, 11)]), ())


if __name__ == "__main__":
    unittest.main()