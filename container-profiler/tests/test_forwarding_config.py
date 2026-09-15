import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.forwarding import DurableDeliveryQueue
from profiler.core.forwarding_config import (
    ForwardingConfigError,
    load_forwarding_config,
    parse_forwarding_config,
)
from profiler.core.metric_forwarding import enqueue_metric_points


class ForwardingConfigTests(unittest.TestCase):
    def test_missing_config_is_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            config = load_forwarding_config(Path(d) / "missing.json", env={})
        self.assertFalse(config.enabled)
        self.assertEqual(config.endpoints, {})

    def test_enabled_https_config_resolves_secret_header_from_environment(self):
        config = parse_forwarding_config({
            "enabled": True,
            "metrics_endpoint": "https://intake.example.test/v1/metrics",
            "headers": {"X-Agent": "container-profiler"},
            "headers_from_env": {"Authorization": "TEST_FORWARD_TOKEN"},
            "batch_points": 100,
        }, env={"TEST_FORWARD_TOKEN": "Bearer secret"})
        self.assertTrue(config.enabled)
        self.assertEqual(
            config.endpoints, {"metrics": "https://intake.example.test/v1/metrics"}
        )
        self.assertEqual(config.header_mapping["Authorization"], "Bearer secret")
        self.assertEqual(config.batch_points, 100)

    def test_disabled_config_does_not_require_secret_environment_value(self):
        config = parse_forwarding_config({
            "enabled": False,
            "metrics_endpoint": "https://intake.example.test/v1/metrics",
            "headers_from_env": {"Authorization": "MISSING_UNTIL_ENABLED"},
        }, env={})
        self.assertFalse(config.enabled)
        self.assertNotIn("Authorization", config.header_mapping)

    def test_external_plain_http_is_rejected_unless_explicitly_allowed(self):
        raw = {
            "enabled": True,
            "metrics_endpoint": "http://10.0.0.5:8080/metrics",
        }
        with self.assertRaisesRegex(ForwardingConfigError, "plain HTTP"):
            parse_forwarding_config(raw, env={})
        config = parse_forwarding_config({**raw, "allow_insecure_http": True}, env={})
        self.assertEqual(config.metrics_endpoint, raw["metrics_endpoint"])

    def test_loopback_http_is_allowed_for_local_development(self):
        config = parse_forwarding_config({
            "enabled": True,
            "metrics_endpoint": "http://127.0.0.1:8000/metrics",
        }, env={})
        self.assertTrue(config.enabled)

    def test_unknown_field_embedded_credentials_and_enabled_missing_env_rejected(self):
        with self.assertRaisesRegex(ForwardingConfigError, "unknown"):
            parse_forwarding_config({"wat": True}, env={})
        with self.assertRaisesRegex(ForwardingConfigError, "credentials"):
            parse_forwarding_config({
                "enabled": True,
                "metrics_endpoint": "https://u:p@example.test/metrics",
            }, env={})
        with self.assertRaisesRegex(ForwardingConfigError, "not set"):
            parse_forwarding_config({
                "enabled": True,
                "metrics_endpoint": "https://example.test/metrics",
                "headers_from_env": {"Authorization": "MISSING"},
            }, env={})

    def test_retry_cap_must_not_be_below_base_delay(self):
        with self.assertRaisesRegex(ForwardingConfigError, "max_delay_s"):
            parse_forwarding_config({"base_delay_s": 10, "max_delay_s": 5}, env={})

    def test_config_file_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forwarding.json"
            path.write_text(json.dumps({
                "enabled": False,
                "metrics_endpoint": "https://example.test/metrics",
                "batch_points": 42,
            }), encoding="utf-8")
            config = load_forwarding_config(path, env={})
        self.assertFalse(config.enabled)
        self.assertEqual(config.batch_points, 42)


class MetricBatchTests(unittest.TestCase):
    @staticmethod
    def point(i: int, *, tag_value: str = "web") -> CustomMetricPoint:
        return CustomMetricPoint(
            timestamp=1000.0 + i,
            name="demo.requests_total",
            value=float(i),
            tags=(f"service:{tag_value}", "env:test"),
            metric_type="counter",
            unit="request",
            source="openmetrics",
            target_key="abc:openmetrics:0",
            container_id="abc",
        )

    def test_points_are_batched_by_count_into_durable_queue(self):
        with DurableDeliveryQueue(":memory:") as queue:
            result = enqueue_metric_points(
                queue,
                [self.point(i) for i in range(5)],
                max_points_per_batch=2,
                max_payload_bytes=4096,
            )
            self.assertEqual(result.input_points, 5)
            self.assertEqual(result.batches_enqueued, 3)
            self.assertEqual(result.points_enqueued, 5)
            self.assertEqual(result.points_dropped, 0)
            payloads = [json.loads(item.payload) for item in queue.due(limit=10)]
            self.assertEqual([len(item["points"]) for item in payloads], [2, 2, 1])
            self.assertEqual(payloads[0]["version"], 1)
            self.assertEqual(payloads[0]["kind"], "metrics")
            self.assertEqual(payloads[0]["points"][0]["container_id"], "abc")

    def test_payload_byte_limit_splits_batches(self):
        with DurableDeliveryQueue(":memory:") as queue:
            points = [self.point(i, tag_value="x" * 80) for i in range(3)]
            single_size = len(json.dumps({
                "version": 1,
                "kind": "metrics",
                "points": [{
                    "timestamp": 1000.0,
                    "name": "demo.requests_total",
                    "value": 0.0,
                    "tags": ["service:" + "x" * 80, "env:test"],
                    "source": "openmetrics",
                    "type": "counter",
                    "unit": "request",
                    "target_key": "abc:openmetrics:0",
                    "container_id": "abc",
                }],
            }, separators=(",", ":")).encode())
            result = enqueue_metric_points(
                queue,
                points,
                max_points_per_batch=100,
                max_payload_bytes=single_size + 20,
            )
            self.assertEqual(result.batches_enqueued, 3)
            self.assertEqual(result.points_enqueued, 3)

    def test_single_oversized_point_is_dropped_and_later_points_continue(self):
        huge = CustomMetricPoint(
            timestamp=1.0,
            name="huge.metric",
            value=1.0,
            tags=("blob:" + "x" * 5000,),
        )
        small = self.point(2)
        with DurableDeliveryQueue(":memory:") as queue:
            result = enqueue_metric_points(queue, (huge, small), max_payload_bytes=1024)
            self.assertEqual(result.points_dropped, 1)
            self.assertEqual(result.points_enqueued, 1)
            self.assertEqual(len(queue.due()), 1)


if __name__ == "__main__":
    unittest.main()
