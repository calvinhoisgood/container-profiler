from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import agent


class AgentCLITests(unittest.TestCase):
    def test_self_metrics_interval_is_configurable_and_validated(self):
        args = agent.build_parser().parse_args(["--self-metrics-interval", "3.5"])
        self.assertEqual(args.self_metrics_interval, 3.5)
        with self.assertRaises(SystemExit):
            agent.main(["--self-metrics-interval", "0", "--status"])

    def test_metric_alert_config_builds_optional_bounded_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "alerts.json"
            config.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "name": "hot",
                                "metric": "custom.temperature",
                                "operator": "gt",
                                "threshold": 80,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = agent.build_parser().parse_args(
                [
                    "--metric-alerts-config",
                    str(config),
                    "--metric-alerts-max-series",
                    "17",
                    "--no-container-metrics",
                    "--no-dogstatsd",
                    "--no-openmetrics",
                    "--no-forwarding",
                ]
            )
            runtime = agent._runtime_for(object(), args)
            self.assertIsNotNone(runtime.metric_alert_runtime)
            self.assertEqual(runtime.metric_alert_runtime.max_series, 17)
            self.assertEqual(runtime.metric_alert_runtime.snapshot().rule_count, 1)

    def test_metric_alert_series_bound_is_validated(self):
        with self.assertRaises(SystemExit):
            agent.main(["--metric-alerts-max-series", "0", "--status"])

    def test_status_surfaces_self_metric_progress_and_failure(self):
        view = SimpleNamespace(
            runtime={
                "ticks": 12,
                "host_samples_persisted": 1,
                "system_points_persisted": 2,
                "container_points_persisted": 3,
                "statsd_points_persisted": 4,
                "openmetrics_points_persisted": 5,
                "alert_events_persisted": 7,
                "self_points_persisted": 6,
                "last_self_storage_error": "sqlite busy",
                "metric_alerts": {
                    "rule_count": 2,
                    "tracked_series": 3,
                    "evicted_series": 1,
                    "last_config_error": "bad replacement",
                },
            },
            age_s=1.0,
            stale=False,
            state="running",
            pid=42,
        )
        with mock.patch.object(agent, "read_agent_status", return_value=view), mock.patch(
            "builtins.print"
        ) as printer:
            result = agent._print_status(Path("unused.json"), 15.0)
        self.assertEqual(result, 0)
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("alerts=7", output)
        self.assertIn("self_points=6", output)
        self.assertIn("metric_alerts=rules:2 series:3 evicted:1", output)
        self.assertIn("metric_alerts_config_error=bad replacement", output)
        self.assertIn("last_self_storage_error=sqlite busy", output)


if __name__ == "__main__":
    unittest.main()