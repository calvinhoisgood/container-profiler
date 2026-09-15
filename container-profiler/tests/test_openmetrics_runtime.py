import json
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.openmetrics_runtime import (
    AD_CHECKS_LABEL,
    AD_CHECK_NAMES_LABEL,
    AD_INSTANCES_LABEL,
    OpenMetricsCheckScheduler,
    OpenMetricsDiscoveryError,
    discover_openmetrics_targets,
)


class DiscoveryTests(unittest.TestCase):
    def test_v2_discovers_and_resolves_safe_container_endpoint(self):
        labels = {
            AD_CHECKS_LABEL: json.dumps({
                "openmetrics": {
                    "instances": [{
                        "openmetrics_endpoint": "http://%%host%%:%%port%%/metrics",
                        "namespace": "demo",
                        "metrics": ["requests_.*", {"temperature_c": "temperature"}],
                        "min_collection_interval": 30,
                        "max_returned_metrics": 123,
                        "tags": ["source:prometheus"],
                    }]
                }
            })
        }
        result = discover_openmetrics_targets(
            labels, container_key="abc", inherited_tags=("service:web",)
        )
        self.assertEqual(result.errors, ())
        target = result.targets[0]
        self.assertEqual(target.metric_patterns, ("requests_.*", "temperature_c"))
        self.assertEqual(target.metric_renames, (("temperature_c", "temperature"),))
        self.assertEqual(target.interval_s, 30.0)
        self.assertEqual(target.max_samples, 123)
        self.assertEqual(target.tags, ("service:web", "source:prometheus"))
        resolved = target.resolve(host="172.17.0.2", port=9100)
        self.assertEqual(resolved.url, "http://172.17.0.2:9100/metrics")

    def test_v2_precedes_legacy_when_openmetrics_present(self):
        labels = {
            AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
                "openmetrics_endpoint": "http://%%host%%:9000/v2",
                "metrics": ["v2_metric"],
            }]}}),
            AD_CHECK_NAMES_LABEL: '["openmetrics"]',
            AD_INSTANCES_LABEL: json.dumps([{
                "openmetrics_endpoint": "http://%%host%%:9001/legacy",
                "metrics": ["legacy_metric"],
            }]),
        }
        result = discover_openmetrics_targets(labels, container_key="c")
        self.assertEqual(len(result.targets), 1)
        self.assertIn("/v2", result.targets[0].endpoint_template)

    def test_legacy_supports_nested_json_instance(self):
        instance = json.dumps({
            "openmetrics_endpoint": "http://%%host%%:9100/metrics",
            "namespace": "node",
            "metrics": ["node_.*"],
        })
        result = discover_openmetrics_targets({
            AD_CHECK_NAMES_LABEL: '["redisdb","openmetrics"]',
            AD_INSTANCES_LABEL: json.dumps([{}, instance]),
        }, container_key="node1")
        self.assertEqual(result.errors, ())
        self.assertEqual(result.targets[0].namespace, "node")

    def test_bad_instance_isolated_from_valid_instance(self):
        labels = {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [
            {"openmetrics_endpoint": "http://%%host%%:1/metrics", "metrics": ["ok"]},
            {"openmetrics_endpoint": "http://%%host%%:2/metrics", "metrics": ["["]},
        ]}})}
        result = discover_openmetrics_targets(labels, container_key="c")
        self.assertEqual(len(result.targets), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("invalid metric regex", result.errors[0])

    def test_external_endpoint_rejected_by_default(self):
        labels = {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://169.254.169.254/latest/meta-data",
            "metrics": [".*"],
        }]}})}
        target = discover_openmetrics_targets(labels, container_key="evil").targets[0]
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "must use %%host%%"):
            target.resolve(host="172.17.0.5")
        self.assertEqual(
            target.resolve(host="172.17.0.5", allow_external=True).url,
            "http://169.254.169.254/latest/meta-data",
        )

    def test_credentials_unknown_template_and_missing_port_rejected(self):
        labels = {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://user:pass@%%host%%:%%port%%/metrics",
            "metrics": ["x"],
        }]}})}
        target = discover_openmetrics_targets(labels, container_key="c").targets[0]
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "credentials"):
            target.resolve(host="10.0.0.2", port=9000)

        target2 = target.__class__(
            key=target.key,
            endpoint_template="http://%%host%%:%%port%%/metrics",
            namespace="",
            metric_patterns=("x",),
        )
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "no port"):
            target2.resolve(host="10.0.0.2")

        target3 = target.__class__(
            key=target.key,
            endpoint_template="http://%%host%%:9000/%%env_TOKEN%%",
            namespace="",
            metric_patterns=("x",),
        )
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "unsupported template"):
            target3.resolve(host="10.0.0.2")

    def test_target_count_and_metric_count_are_bounded(self):
        instances = [
            {"openmetrics_endpoint": f"http://%%host%%:{9000+i}/metrics", "metrics": ["x"]}
            for i in range(17)
        ]
        result = discover_openmetrics_targets(
            {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": instances}})},
            container_key="c",
        )
        self.assertEqual(result.targets, ())
        self.assertIn("target count exceeds", result.errors[0])

        too_many = [f"metric_{i}" for i in range(257)]
        result = discover_openmetrics_targets(
            {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
                "openmetrics_endpoint": "http://%%host%%:9000/metrics",
                "metrics": too_many,
            }]}})},
            container_key="c",
        )
        self.assertEqual(result.targets, ())
        self.assertIn("metrics list exceeds", result.errors[0])


@dataclass
class FakeResult:
    samples: tuple[int, ...]
    payload_bytes: int


class FakeCollector:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def scrape(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.fail:
            raise RuntimeError("endpoint down")
        return FakeResult((1, 2), 42)


class SchedulerTests(unittest.TestCase):
    def discovery_result(self):
        return discover_openmetrics_targets({AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://%%host%%:9000/metrics",
            "namespace": "demo",
            "metrics": ["x.*"],
        }]}})}, container_key="c")

    def make_target(self, result, *, key="c:openmetrics:0", port=9000, interval=15):
        template = result.targets[0]
        target = template.resolve(host="10.0.0.2")
        return target.__class__(
            key=key,
            url=f"http://10.0.0.2:{port}/metrics",
            namespace=target.namespace,
            metric_patterns=target.metric_patterns,
            metric_renames=target.metric_renames,
            interval_s=interval,
            max_samples=target.max_samples,
            tags=target.tags,
        )

    def test_due_schedule_has_no_catch_up_storm(self):
        collector = FakeCollector()
        scheduler = OpenMetricsCheckScheduler(collector)
        target = self.make_target(self.discovery_result(), interval=15)
        scheduler.replace_targets([target], now=100)
        self.assertEqual(len(scheduler.run_due(now=100)), 1)
        self.assertEqual(scheduler.run_due(now=114.9), ())
        self.assertEqual(len(scheduler.run_due(now=200)), 1)
        health = scheduler.snapshot()[0]
        self.assertEqual(health.next_due, 215.0)
        self.assertEqual(health.successful_scrapes, 2)
        self.assertEqual(health.last_sample_count, 2)
        self.assertEqual(health.last_payload_bytes, 42)

    def test_failure_isolated_and_health_preserved_across_unchanged_sync(self):
        collector = FakeCollector()
        scheduler = OpenMetricsCheckScheduler(collector)
        result = self.discovery_result()
        good = self.make_target(result, key="good", port=9000)
        bad = self.make_target(result, key="bad", port=9001)
        collector.fail.add(bad.url)
        scheduler.replace_targets([bad, good], now=0)
        batches = scheduler.run_due(now=0)
        self.assertEqual([batch.target.key for batch in batches], ["good"])
        health = {item.key: item for item in scheduler.snapshot()}
        self.assertEqual(health["bad"].failed_scrapes, 1)
        self.assertEqual(health["bad"].last_error, "endpoint down")
        self.assertEqual(health["good"].successful_scrapes, 1)

        scheduler.replace_targets([bad, good], now=1)
        health2 = {item.key: item for item in scheduler.snapshot()}
        self.assertEqual(health2["bad"].failed_scrapes, 1)
        self.assertEqual(scheduler.run_due(now=1), ())

    def test_changed_target_resets_only_its_schedule_and_removed_target_disappears(self):
        collector = FakeCollector()
        scheduler = OpenMetricsCheckScheduler(collector)
        result = self.discovery_result()
        first = self.make_target(result, key="one", port=9000)
        second = self.make_target(result, key="two", port=9001)
        scheduler.replace_targets([first, second], now=0)
        scheduler.run_due(now=0)

        changed = self.make_target(result, key="one", port=9999)
        scheduler.replace_targets([changed], now=5)
        snapshot = scheduler.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0].key, "one")
        self.assertEqual(snapshot[0].successful_scrapes, 0)
        self.assertEqual(snapshot[0].next_due, 5.0)

    def test_duplicate_target_keys_rejected(self):
        scheduler = OpenMetricsCheckScheduler(FakeCollector())
        target = self.make_target(self.discovery_result())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            scheduler.replace_targets([target, target], now=0)


if __name__ == "__main__":
    unittest.main()
