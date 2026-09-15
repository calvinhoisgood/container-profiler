from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_self_metrics import normalize_agent_self_metrics


class AgentLogSelfMetricTests(unittest.TestCase):
    def test_log_pipeline_health_is_queryable_as_agent_metrics(self):
        snapshot = SimpleNamespace(
            ticks=1,
            logs_persisted=11,
            log_storage_failures=2,
            logs=SimpleNamespace(
                discovery_failures=3,
                sample_failures=4,
                records_collected=12,
                duplicate_records=5,
                untimestamped_records=6,
                buffer=SimpleNamespace(queued_records=7, dropped_records=8),
            ),
        )
        points = normalize_agent_self_metrics(snapshot, timestamp=100.0, hostname="node-a")
        indexed = {(point.name, point.tags): point.value for point in points}
        self.assertEqual(
            indexed[("container_profiler.agent.persisted", ("host:node-a", "subsystem:logs"))],
            11.0,
        )
        self.assertEqual(
            indexed[("container_profiler.agent.storage_failures", ("host:node-a", "subsystem:logs"))],
            2.0,
        )
        by_name = {point.name: point.value for point in points}
        self.assertEqual(by_name["container_profiler.logs.discovery_failures"], 3.0)
        self.assertEqual(by_name["container_profiler.logs.buffer_dropped_records"], 8.0)


if __name__ == "__main__":
    unittest.main()
