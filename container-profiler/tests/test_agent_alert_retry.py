import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.metric_alerts import MetricAlertRuntime


class EmptyHost:
    def drain(self, limit):
        return ()

    def requeue_front(self, items):
        return 0

    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True


class MetricWorker:
    def __init__(self, items):
        self.items = list(items)

    def drain_points(self, limit):
        values = self.items[:limit]
        del self.items[:limit]
        return tuple(values)

    def requeue_points(self, points):
        values = list(points)
        self.items[:0] = values
        return len(values)

    def start(self):
        pass

    def stop(self, timeout_s=2.0):
        return True


class RecoveringStore:
    def __init__(self):
        self.fail_alerts = True
        self.alerts = []
        self.metric_rows = 0

    def append_host_samples(self, samples):
        return len(tuple(samples))

    def append_custom_metrics(self, points):
        values = tuple(points)
        self.metric_rows += len(values)
        return len(values)

    def append_alert_event(self, event, **kwargs):
        if self.fail_alerts:
            raise OSError("alert store unavailable")
        self.alerts.append((event, kwargs))
        return 1

    def prune_host_samples(self, **kwargs):
        return 0

    def prune_custom_metrics(self, **kwargs):
        return 0


def config(directory):
    path = Path(directory) / "alerts.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "hot",
                        "metric": "temperature",
                        "operator": "gt",
                        "threshold": 80,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def hot_point(target):
    return CustomMetricPoint(
        timestamp=time.time(),
        name="temperature",
        value=90,
        tags=(f"target:{target}",),
        source="test",
        target_key=target,
        container_id=target,
    )


class AlertRetryTests(unittest.TestCase):
    def test_failed_alert_events_retry_without_requeueing_metrics_and_stay_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveringStore()
            metrics = MetricWorker([hot_point("a"), hot_point("b"), hot_point("c")])
            runtime = LocalAgentRuntime(
                store,
                host_worker=EmptyHost(),
                system_worker=metrics,
                metric_alert_runtime=MetricAlertRuntime(config(directory)),
                alert_queue_max=2,
            )

            runtime.run_once()
            failed = runtime.snapshot()
            self.assertEqual(store.metric_rows, 3)
            self.assertEqual(metrics.items, [])
            self.assertEqual(failed.alert_events_queued, 2)
            self.assertEqual(failed.alert_events_dropped, 1)
            self.assertEqual(failed.alert_events_persisted, 0)
            self.assertEqual(failed.alert_failures, 1)

            store.fail_alerts = False
            runtime.run_once()
            recovered = runtime.snapshot()
            self.assertEqual(store.metric_rows, 3)
            self.assertEqual(recovered.alert_events_queued, 0)
            self.assertEqual(recovered.alert_events_dropped, 1)
            self.assertEqual(recovered.alert_events_persisted, 2)
            self.assertEqual(len(store.alerts), 2)
            self.assertIsNone(recovered.last_alert_error)


if __name__ == "__main__":
    unittest.main()
