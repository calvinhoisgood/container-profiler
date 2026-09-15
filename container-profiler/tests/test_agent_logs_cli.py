from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import agent


class AgentLogCLITests(unittest.TestCase):
    def test_logs_are_privacy_safe_opt_in_with_bounded_controls(self):
        parser = agent.build_parser()
        defaults = parser.parse_args([])
        self.assertFalse(defaults.container_logs)
        args = parser.parse_args([
            "--container-logs",
            "--container-log-interval", "3",
            "--container-log-max-containers", "7",
            "--container-log-tail", "123",
            "--container-log-lookback", "15",
        ])
        self.assertTrue(args.container_logs)
        self.assertEqual(args.container_log_interval, 3.0)
        self.assertEqual(args.container_log_max_containers, 7)
        self.assertEqual(args.container_log_tail, 123)
        self.assertEqual(args.container_log_lookback, 15.0)


if __name__ == "__main__":
    unittest.main()
