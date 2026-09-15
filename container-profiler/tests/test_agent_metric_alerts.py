import json
import tempfile
import unittest
from pathlib import Path

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.metric_alerts import MetricAlertRuntime
from profiler.core.storage import SQLiteTelemetryStore


class EmptyHost:
    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def drain(self, limit):
        return ()

    def requeue_front(self, items):
        return len(tuple(items))


class MetricWorker:
    def __init__(self, points=()):
        self.items = list(points)

    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True

    def drain_points(self, limit):
        result = self.items[:limit]
        del self.items[:limit]
        return tuple(result)

    def requeue_points(self, points):
        values = list(points)
        self.items[:0] = values
        return len(values)


def write_config(directory):
    path = Path(directory) / "metric-alerts.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "name": "too-hot",
                        "metric": "custom.temperature",
                        "operator": "gt",
                        "threshold": 80,
                        "recovery_threshold": 75,
                        "severity": "critical",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def temperature(value, timestamp=100.0):
    return CustomMetricPoint(
        timestamp=timestamp,
        name="custom.temperature",
        value=value,
        tags=("service:api",),
        metric_type="gauge",
        unit="celsius",
        source="openmetrics",
        target_key="container:abc:metrics",
        container_id="abc",
    )


class AgentMetricAlertTests(unittest.TestCase):
    def test_persisted_metric_can_emit_durable_alert_event(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteTelemetryStore() as store:
            alerts = MetricAlertRuntime(write_config(directory))
            system = MetricWorker([temperature(90)])
            runtime = LocalAgentRuntime(
                store,
                host_worker=EmptyHost(),
                system_worker=system,
                metric_alert_runtime=alerts,
            )
            runtime.run_once()

            rows = store.query_custom_metrics(name="custom.temperature")
            events = store.query_alert_events(rule_name="too-hot")
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["container_id"], "abc")
            self.assertEqual(events[0]["current_status"], "firing")
            self.assertIn("target=container:abc:metrics", events[0]["message"])
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.alert_events_persisted, 1)
            self.assertEqual(snapshot.alert_failures, 0)
            self.assertEqual(snapshot.metric_alerts.rule_count, 1)
            self.assertEqual(snapshot.metric_alerts.tracked_series, 1)

    def test_failed_metric_persistence_does_not_advance_alert_state(self):
        class MetricFailingStore:
            def append_custom_metrics(self, points):
                raise OSError("metric sqlite busy")

            def append_host_samples(self, items):
                return len(tuple(items))

            def prune_host_samples(self, **kwargs):
                return 0

            def prune_custom_metrics(self, **kwargs):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            alerts = MetricAlertRuntime(write_config(directory))
            system = MetricWorker([temperature(90)])
            runtime = LocalAgentRuntime(
                MetricFailingStore(),
                host_worker=EmptyHost(),
                system_worker=system,
                metric_alert_runtime=alerts,
            )
            runtime.run_once()
            self.assertEqual(runtime.snapshot().system_storage_failures, 1)
            self.assertEqual(runtime.snapshot().metric_alerts.tracked_series, 0)
            self.assertEqual(len(system.items), 1)

    def test_alert_sink_failure_isolated_after_metric_commit(self):
        class AlertFailingStore:
            def __init__(self):
                self.metric_rows = 0

            def append_custom_metrics(self, points):
                values = tuple(points)
                self.metric_rows += len(values)
                return len(values)

            def append_alert_event(self, event, **kwargs):
                raise OSError("alert sqlite busy")

            def append_host_samples(self, items):
                return len(tuple(items))

            def prune_host_samples(self, **kwargs):
                return 0

            def prune_custom_metrics(self, **kwargs):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            store = AlertFailingStore()
            system = MetricWorker([temperature(90)])
            runtime = LocalAgentRuntime(
                store,
                host_worker=EmptyHost(),
                system_worker=system,
                metric_alert_runtime=MetricAlertRuntime(write_config(directory)),
            )
            runtime.run_once()
            snapshot = runtime.snapshot()
            self.assertEqual(store.metric_rows, 1)
            self.assertEqual(snapshot.system_points_persisted, 1)
            self.assertEqual(snapshot.alert_events_persisted, 0)
            self.assertEqual(snapshot.alert_failures, 1)
            self.assertIn("alert sqlite busy", snapshot.last_alert_error)
            self.assertEqual(system.items, [])


if __name__ == "__main__":
    unittest.main()