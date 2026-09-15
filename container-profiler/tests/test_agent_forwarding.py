from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_forwarding import AgentForwardingRuntime
from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.custom_metrics import CustomMetricPoint


class FakeQueue:
    instances = []

    def __init__(self, path, **kwargs):
        self.path = Path(path)
        self.kwargs = kwargs
        self.items = []
        self.closed = False
        FakeQueue.instances.append(self)

    def enqueue(self, kind, payload, **kwargs):
        del kwargs
        self.items.append((kind, bytes(payload)))
        return True

    def stats(self):
        return {
            "queued_items": len(self.items),
            "queued_bytes": sum(len(payload) for _, payload in self.items),
            "dropped_items": 0,
        }

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, endpoints, **kwargs):
        self.endpoints = endpoints
        self.kwargs = kwargs


class FakeDeliveryWorker:
    instances = []

    def __init__(self, queue, transport, **kwargs):
        self.queue = queue
        self.transport = transport
        self.kwargs = kwargs
        self.running = False
        FakeDeliveryWorker.instances.append(self)

    def start(self):
        self.running = True

    def stop(self, timeout_s=6.0):
        del timeout_s
        self.running = False
        return True

    def snapshot(self):
        return {"running": self.running}


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
        out = self.items[:limit]
        del self.items[:limit]
        return tuple(out)

    def requeue_points(self, points):
        values = list(points)
        self.items[:0] = values
        return len(values)


class Store:
    def __init__(self, fail=False):
        self.fail = fail

    def append_host_samples(self, items):
        return len(tuple(items))

    def append_custom_metrics(self, items):
        if self.fail:
            raise OSError("sqlite busy")
        return len(tuple(items))

    def prune_host_samples(self, **kwargs):
        return 0

    def prune_custom_metrics(self, **kwargs):
        return 0


class RecordingForwarder:
    def __init__(self):
        self.items = []
        self.started = False
        self.stopped = False
        self.ticks = 0

    def start(self):
        self.started = True

    def stop(self, timeout_s=6.0):
        self.stopped = True
        return True

    def tick(self):
        self.ticks += 1

    def enqueue(self, points):
        self.items.extend(points)

    def snapshot(self):
        return {"started": self.started, "stopped": self.stopped}


def point():
    return CustomMetricPoint(
        timestamp=1.0,
        name="jobs",
        value=2.0,
        tags=("service:worker",),
        metric_type="count",
        source="dogstatsd",
    )


class AgentForwardingRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeQueue.instances.clear()
        FakeDeliveryWorker.instances.clear()

    def make_runtime(self, root: Path):
        return AgentForwardingRuntime(
            root / "forwarding.json",
            root / "spool.sqlite3",
            queue_factory=FakeQueue,
            transport_factory=FakeTransport,
            worker_factory=FakeDeliveryWorker,
        )

    def write(self, path: Path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_hot_reload_enable_invalid_last_known_good_and_disable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "forwarding.json"
            runtime = self.make_runtime(root)
            runtime.start()
            self.assertFalse(runtime.snapshot().enabled)

            self.write(
                config_path,
                {
                    "enabled": True,
                    "metrics_endpoint": "https://metrics.example.test/v1",
                    "poll_interval_s": 60,
                },
            )
            self.assertTrue(runtime.reload(force=True))
            first_queue = runtime.queue
            self.assertTrue(runtime.snapshot().enabled)
            self.assertTrue(runtime.worker.running)

            result = runtime.enqueue((point(),))
            self.assertEqual(result.points_enqueued, 1)
            self.assertEqual(len(first_queue.items), 1)
            kind, raw = first_queue.items[0]
            self.assertEqual(kind, "metrics")
            self.assertEqual(json.loads(raw)["points"][0]["name"], "jobs")

            config_path.write_text("{not-json", encoding="utf-8")
            self.assertFalse(runtime.reload(force=True))
            self.assertIs(runtime.queue, first_queue)
            self.assertTrue(runtime.snapshot().enabled)
            self.assertIsNotNone(runtime.snapshot().last_config_error)

            self.write(config_path, {"enabled": False})
            self.assertTrue(runtime.reload(force=True))
            self.assertFalse(runtime.snapshot().enabled)
            self.assertTrue(first_queue.closed)
            self.assertIsNone(runtime.queue)
            self.assertTrue(runtime.stop())

    def test_unchanged_signature_does_not_rebuild_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "forwarding.json"
            self.write(config_path, {"enabled": False})
            runtime = self.make_runtime(root)
            runtime.start()
            reloads = runtime.snapshot().reloads
            self.assertFalse(runtime.reload())
            self.assertEqual(runtime.snapshot().reloads, reloads)
            runtime.stop()


class LocalAgentForwardingTests(unittest.TestCase):
    def test_forwarding_happens_only_after_local_persistence(self):
        forwarder = RecordingForwarder()
        metrics = MetricWorker((point(),))
        runtime = LocalAgentRuntime(
            Store(),
            host_worker=EmptyHost(),
            system_worker=metrics,
            forwarding_worker=forwarder,
        )
        runtime.start()
        runtime.run_once()
        self.assertEqual(len(forwarder.items), 1)
        self.assertTrue(forwarder.started)
        self.assertTrue(runtime.stop())
        self.assertTrue(forwarder.stopped)

    def test_failed_local_persistence_requeues_and_does_not_forward(self):
        forwarder = RecordingForwarder()
        metrics = MetricWorker((point(),))
        runtime = LocalAgentRuntime(
            Store(fail=True),
            host_worker=EmptyHost(),
            system_worker=metrics,
            forwarding_worker=forwarder,
        )
        runtime.run_once()
        self.assertEqual(len(metrics.items), 1)
        self.assertEqual(forwarder.items, [])
        self.assertEqual(runtime.snapshot().system_storage_failures, 1)


if __name__ == "__main__":
    unittest.main()
