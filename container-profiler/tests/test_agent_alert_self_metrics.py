import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_self_metrics import normalize_agent_self_metrics


class AlertSelfMetricsTests(unittest.TestCase):
    def test_alert_health_is_queryable_without_exposing_config_text(self):
        snapshot = SimpleNamespace(
            ticks=1,
            host_samples_persisted=0,
            system_points_persisted=0,
            container_points_persisted=0,
            statsd_points_persisted=0,
            openmetrics_points_persisted=0,
            alert_events_persisted=9,
            host_storage_failures=0,
            system_storage_failures=0,
            container_storage_failures=0,
            statsd_storage_failures=0,
            openmetrics_storage_failures=0,
            self_storage_failures=2,
            retention_failures=0,
            alert_failures=3,
            host=None,
            system=None,
            statsd=None,
            openmetrics=None,
            containers=None,
            forwarding=None,
            metric_alerts=SimpleNamespace(
                rule_count=4,
                tracked_series=12,
                event_count=7,
                evicted_series=5,
                invalid_points=6,
                last_config_error="invalid secret-looking config details",
            ),
        )
        points = normalize_agent_self_metrics(
            snapshot,
            timestamp=10.0,
            hostname="node-a",
        )
        keyed = {(item.name, tuple(item.tags)): item.value for item in points}
        self.assertEqual(
            keyed[
                (
                    "container_profiler.agent.persisted",
                    ("host:node-a", "subsystem:alerts"),
                )
            ],
            9.0,
        )
        self.assertEqual(
            keyed[("container_profiler.alert.failures", ("host:node-a",))],
            3.0,
        )
        self.assertEqual(
            keyed[("container_profiler.alert.tracked_series", ("host:node-a",))],
            12.0,
        )
        self.assertEqual(
            keyed[("container_profiler.alert.evicted_series", ("host:node-a",))],
            5.0,
        )
        self.assertEqual(
            keyed[("container_profiler.alert.config_error", ("host:node-a",))],
            1.0,
        )
        # Error detail remains in status, while self metrics expose only a safe bit.
        self.assertTrue(
            all("invalid secret" not in tag for item in points for tag in item.tags)
        )


if __name__ == "__main__":
    unittest.main()