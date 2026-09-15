import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.forwarding import (
    DeliveryResponse,
    DeliveryWorker,
    DurableDeliveryQueue,
    is_retryable_http_status,
)


class DurableDeliveryQueueTests(unittest.TestCase):
    def test_persists_across_restart_and_acknowledges_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forward.db"
            with DurableDeliveryQueue(path, clock=lambda: 10.0) as queue:
                self.assertTrue(queue.enqueue("metrics", b"abc"))
                item = queue.due()[0]
                self.assertEqual(item.payload, b"abc")
            with DurableDeliveryQueue(path, clock=lambda: 11.0) as queue:
                item = queue.due()[0]
                self.assertTrue(queue.acknowledge(item.id))
                self.assertFalse(queue.acknowledge(item.id))
                self.assertEqual(queue.due(), ())

    def test_cumulative_health_counters_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forward.db"
            with DurableDeliveryQueue(path, clock=lambda: 10.0) as queue:
                queue.enqueue("metrics", b"ok")
                delivered = queue.due()[0]
                self.assertTrue(queue.acknowledge(delivered.id))
                queue.enqueue("metrics", b"retry")
                retry = queue.due()[0]
                queue.retry(retry.id, now=10.0, jitter=0.0)
                queue.enqueue("metrics", b"drop")
                drop = [x for x in queue.due(now=10.0) if x.payload == b"drop"][0]
                queue.discard(drop.id)
            with DurableDeliveryQueue(path, clock=lambda: 20.0) as queue:
                stats = queue.stats()
                self.assertEqual(stats.delivered_items, 1)
                self.assertEqual(stats.failed_attempts, 1)
                self.assertEqual(stats.dropped_items, 1)

    def test_drop_oldest_enforces_item_and_byte_bounds(self):
        with DurableDeliveryQueue(":memory:", max_items=3, max_bytes=8) as queue:
            for value in (b"aa", b"bb", b"cc", b"dddd"):
                self.assertTrue(queue.enqueue("metrics", value))
            items = queue.due(limit=10)
            self.assertEqual([item.payload for item in items], [b"bb", b"cc", b"dddd"])
            stats = queue.stats()
            self.assertEqual(stats.queued_items, 3)
            self.assertEqual(stats.queued_bytes, 8)
            self.assertEqual(stats.dropped_items, 1)

    def test_exact_limits_do_not_evict_valid_items(self):
        with DurableDeliveryQueue(":memory:", max_items=2, max_bytes=4) as queue:
            queue.enqueue("metrics", b"aa")
            queue.enqueue("metrics", b"bb")
            self.assertEqual([item.payload for item in queue.due()], [b"aa", b"bb"])
            self.assertEqual(queue.stats().dropped_items, 0)

    def test_oversized_payload_is_rejected_without_touching_existing_data(self):
        with DurableDeliveryQueue(":memory:", max_bytes=10, max_payload_bytes=4) as queue:
            self.assertTrue(queue.enqueue("metrics", b"ok"))
            self.assertFalse(queue.enqueue("metrics", b"12345"))
            self.assertEqual([item.payload for item in queue.due()], [b"ok"])
            self.assertEqual(queue.stats().dropped_items, 1)

    def test_retry_uses_exponential_backoff_with_bounded_jitter(self):
        with DurableDeliveryQueue(":memory:", clock=lambda: 100.0) as queue:
            queue.enqueue("metrics", b"payload")
            item = queue.due()[0]
            next_at = queue.retry(item.id, now=100.0, base_delay_s=2.0,
                                  max_delay_s=5.0, jitter=0.25, random_value=0.5)
            self.assertEqual(next_at, 102.0)
            self.assertEqual(queue.due(now=101.99), ())
            retried = queue.due(now=102.0)[0]
            self.assertEqual(retried.attempts, 1)
            next_at = queue.retry(item.id, now=102.0, base_delay_s=2.0,
                                  max_delay_s=5.0, jitter=0.25, random_value=0.5)
            self.assertEqual(next_at, 106.0)
            retried = queue.due(now=106.0)[0]
            self.assertEqual(retried.attempts, 2)
            self.assertEqual(queue.stats(now=106.0).failed_attempts, 2)

    def test_retry_after_is_bounded_by_max_delay(self):
        with DurableDeliveryQueue(":memory:", clock=lambda: 100.0) as queue:
            queue.enqueue("metrics", b"payload")
            item = queue.due()[0]
            next_at = queue.retry(
                item.id, now=100.0, max_delay_s=30.0, retry_after_s=120.0
            )
            self.assertEqual(next_at, 130.0)

    def test_due_is_bounded_and_preserves_fifo_for_equal_deadlines(self):
        with DurableDeliveryQueue(":memory:", clock=lambda: 1.0) as queue:
            for i in range(5):
                queue.enqueue("logs", str(i).encode())
            self.assertEqual([x.payload for x in queue.due(limit=2)], [b"0", b"1"])
            self.assertEqual(queue.due(limit=0), ())

    def test_kind_and_retry_policy_validation(self):
        with DurableDeliveryQueue(":memory:") as queue:
            with self.assertRaises(ValueError):
                queue.enqueue("", b"x")
            queue.enqueue("metrics", b"x")
            item = queue.due()[0]
            with self.assertRaises(ValueError):
                queue.retry(item.id, base_delay_s=0)
            with self.assertRaises(ValueError):
                queue.retry(item.id, retry_after_s=-1)
            self.assertIsNone(queue.retry(999999))


class SequenceTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.items = []

    def send(self, item):
        self.items.append(item)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class DeliveryWorkerTests(unittest.TestCase):
    def test_http_status_classification(self):
        for status in (408, 425, 429, 500, 503, 599):
            self.assertTrue(is_retryable_http_status(status))
        for status in (200, 301, 400, 401, 403, 404, 413, 422):
            self.assertFalse(is_retryable_http_status(status))

    def test_success_retry_and_permanent_failure_have_distinct_semantics(self):
        with DurableDeliveryQueue(":memory:", clock=lambda: 100.0) as queue:
            for payload in (b"success", b"retry", b"bad"):
                queue.enqueue("metrics", payload)
            transport = SequenceTransport(
                DeliveryResponse(202),
                DeliveryResponse(429, retry_after_s=10.0),
                DeliveryResponse(400),
            )
            worker = DeliveryWorker(
                queue, transport, batch_size=10, jitter=0.0, clock=lambda: 100.0
            )
            self.assertEqual(worker.process_once(now=100.0), 3)
            self.assertEqual(queue.due(now=100.0), ())
            retried = queue.due(now=110.0)
            self.assertEqual([x.payload for x in retried], [b"retry"])
            snapshot = worker.snapshot()
            self.assertEqual(snapshot.successful_deliveries, 1)
            self.assertEqual(snapshot.retryable_failures, 1)
            self.assertEqual(snapshot.permanent_failures, 1)
            self.assertEqual(snapshot.queue.delivered_items, 1)
            self.assertEqual(snapshot.queue.failed_attempts, 1)
            self.assertEqual(snapshot.queue.dropped_items, 1)

    def test_network_exception_is_retryable_and_keeps_payload(self):
        with DurableDeliveryQueue(":memory:", clock=lambda: 10.0) as queue:
            queue.enqueue("metrics", b"x")
            worker = DeliveryWorker(
                queue,
                SequenceTransport(OSError("network down")),
                jitter=0.0,
                base_delay_s=2.0,
                clock=lambda: 10.0,
            )
            worker.process_once(now=10.0)
            self.assertEqual(queue.due(now=11.99), ())
            self.assertEqual(queue.due(now=12.0)[0].payload, b"x")
            self.assertEqual(worker.snapshot().last_error, "network down")


if __name__ == "__main__":
    unittest.main()
