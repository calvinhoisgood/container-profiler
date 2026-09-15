from __future__ import annotations

import sys
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

    def test_status_surfaces_self_metric_progress_and_failure(self):
        view = SimpleNamespace(
            runtime={
                "ticks": 12,
                "host_samples_persisted": 1,
                "system_points_persisted": 2,
                "container_points_persisted": 3,
                "statsd_points_persisted": 4,
                "openmetrics_points_persisted": 5,
                "self_points_persisted": 6,
                "last_self_storage_error": "sqlite busy",
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
        self.assertIn("self_points=6", output)
        self.assertIn("last_self_storage_error=sqlite busy", output)


if __name__ == "__main__":
    unittest.main()
