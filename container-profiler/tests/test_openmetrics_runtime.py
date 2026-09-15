import json
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.container_context import (
    ContainerTemplateContext,
    extract_container_template_context,
)
from profiler.core.custom_metrics import BoundedMetricBuffer, normalize_openmetrics_samples
from profiler.core.models import ContainerInfo
from profiler.core.openmetrics_runtime import (
    AD_CHECKS_LABEL,
    AD_CHECK_NAMES_LABEL,
    AD_INSTANCES_LABEL,
    OpenMetricsCheckScheduler,
    OpenMetricsDiscoveryError,
    OpenMetricsRuntime,
    OpenMetricsRuntimeWorker,
    discover_openmetrics_targets,
    resolve_endpoint_template,
    resolve_openmetrics_for_containers,
)


class ContainerContextTests(unittest.TestCase):
    def test_extracts_networks_ports_hostname_and_pid(self):
        attrs = {
            "Config": {
                "Hostname": "worker-1",
                "ExposedPorts": {"80/tcp": {}, "9100/tcp": {}, "bad": {}},
            },
            "HostConfig": {"PortBindings": {"443/tcp": []}},
            "NetworkSettings": {
                "Networks": {
                    "app": {"IPAddress": "10.20.0.4"},
                    "bridge": {"IPAddress": "172.17.0.4"},
                    "v6": {"IPAddress": "", "GlobalIPv6Address": "2001:db8::4"},
                },
                "Ports": {"8080/tcp": None},
            },
            "State": {"Pid": 1234},
        }
        context = extract_container_template_context(attrs)
        self.assertEqual(context.primary_host, "172.17.0.4")
        self.assertEqual(context.host_for_network("app"), "10.20.0.4")
        self.assertEqual(context.host_for_network("missing"), "172.17.0.4")
        self.assertEqual(context.ports, (80, 443, 8080, 9100))
        self.assertEqual(context.hostname, "worker-1")
        self.assertEqual(context.pid, 1234)

    def test_context_handles_missing_or_malformed_sections(self):
        context = extract_container_template_context({
            "NetworkSettings": {"Networks": []},
            "Config": "not-a-map",
            "State": {"Pid": "bad"},
        })
        self.assertIsNone(context.primary_host)
        self.assertEqual(context.network_hosts, ())
        self.assertEqual(context.ports, ())
        self.assertIsNone(context.pid)


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
        self.assertEqual(resolved.container_id, "abc")

    def test_datadog_host_network_and_port_index_semantics(self):
        context = ContainerTemplateContext(
            primary_host="172.17.0.2",
            network_hosts=(("bridge", "172.17.0.2"), ("app", "10.0.0.2")),
            ports=(8443, 80, 443),
            hostname="demo",
            pid=55,
        )
        self.assertEqual(
            resolve_endpoint_template(
                "http://%%host_app%%:%%port_0%%/metrics", context=context
            ),
            "http://10.0.0.2:80/metrics",
        )
        self.assertEqual(
            resolve_endpoint_template(
                "http://%%host%%:%%port%%/metrics?pid=%%pid%%", context=context
            ),
            "http://172.17.0.2:8443/metrics?pid=55",
        )
        # Datadog documents a missing named network as falling back to %%host%%.
        self.assertEqual(
            resolve_endpoint_template(
                "http://%%host_missing%%:9000/metrics", context=context
            ),
            "http://172.17.0.2:9000/metrics",
        )

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

    def test_invalid_alias_is_rejected_per_instance(self):
        labels = {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://%%host%%:9000/metrics",
            "metrics": [{"raw_metric": "bad metric name"}],
        }]}})}
        result = discover_openmetrics_targets(labels, container_key="c")
        self.assertEqual(result.targets, ())
        self.assertIn("metric alias", result.errors[0])

    def test_external_endpoint_rejected_by_default(self):
        labels = {AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://169.254.169.254/latest/meta-data",
            "metrics": [".*"],
        }]}})}
        target = discover_openmetrics_targets(labels, container_key="evil").targets[0]
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "discovered container IP"):
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
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "no exposed port"):
            target2.resolve(host="10.0.0.2")

        target3 = target.__class__(
            key=target.key,
            endpoint_template="http://%%host%%:9000/%%env_TOKEN%%",
            namespace="",
            metric_patterns=("x",),
        )
        with self.assertRaisesRegex(OpenMetricsDiscoveryError, "unsupported template"):
            target3.resolve(host="10.0.0.2")

    def test_ipv6_host_is_bracketed_in_url(self):
        context = ContainerTemplateContext(
            primary_host="2001:db8::5",
            network_hosts=(("v6", "2001:db8::5"),),
            ports=(9100,),
        )
        self.assertEqual(
            resolve_endpoint_template(
                "http://%%host%%:%%port%%/metrics", context=context
            ),
            "http://[2001:db8::5]:9100/metrics",
        )

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

    def test_running_containers_resolve_and_stopped_containers_are_removed(self):
        labels = ((AD_CHECKS_LABEL, json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://%%host%%:%%port%%/metrics",
            "metrics": ["x"],
        }]}})),)
        running = ContainerInfo(
            "abc", "web", "running", "demo", "", labels=labels,
            tags=("service:web",), primary_host="172.17.0.8",
            network_hosts=(("bridge", "172.17.0.8"),), exposed_ports=(8080, 9100),
        )
        stopped = ContainerInfo(
            "def", "old", "exited", "demo", "", labels=labels,
            primary_host="172.17.0.9", exposed_ports=(9100,),
        )
        resolved = resolve_openmetrics_for_containers((running, stopped))
        self.assertEqual(len(resolved.targets), 1)
        self.assertEqual(resolved.targets[0].url, "http://172.17.0.8:9100/metrics")
        self.assertEqual(resolved.targets[0].container_id, "abc")


@dataclass
class FakeResult:
    samples: tuple
    payload_bytes: int


class FakeCollector:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.samples = (1, 2)

    def scrape(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.fail:
            raise RuntimeError("endpoint down")
        return FakeResult(tuple(self.samples), 42)


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
            container_id=target.container_id,
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
        self.assertEqual(collector.calls[0][1]["namespace"], "")

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


class CustomMetricPipelineTests(unittest.TestCase):
    def sample(self, name="temperature_c", value=12.5, timestamp_ms=None):
        return SimpleNamespace(
            name=name,
            value=value,
            tags=("sensor:cpu",),
            metric_type="gauge",
            unit="celsius",
            timestamp_ms=timestamp_ms,
        )

    def target(self):
        result = discover_openmetrics_targets({AD_CHECKS_LABEL: json.dumps({"openmetrics": {"instances": [{
            "openmetrics_endpoint": "http://%%host%%:9000/metrics",
            "namespace": "demo",
            "metrics": [{"temperature_c": "temperature"}],
            "tags": ["service:web"],
        }]}})}, container_key="abc")
        return result.targets[0].resolve(host="10.0.0.2")

    def test_normalization_applies_alias_namespace_tags_and_timestamp(self):
        target = self.target()
        result = FakeResult((self.sample(timestamp_ms=1234000),), 100)
        point = normalize_openmetrics_samples(target, result, collected_at=9.0)[0]
        self.assertEqual(point.name, "demo.temperature")
        self.assertEqual(point.tags, ("service:web", "sensor:cpu"))
        self.assertEqual(point.timestamp, 1234.0)
        self.assertEqual(point.container_id, "abc")
        self.assertEqual(point.metric_type, "gauge")

    def test_buffer_drop_oldest_byte_bound_and_requeue(self):
        target = self.target()
        result = FakeResult((self.sample("temperature_c", 1),), 100)
        point = normalize_openmetrics_samples(target, result, collected_at=1)[0]
        buffer = BoundedMetricBuffer(max_points=2, max_bytes=4096)
        buffer.append_many((point, point, point))
        self.assertEqual(buffer.stats().queued_points, 2)
        self.assertEqual(buffer.stats().dropped_points, 1)
        drained = buffer.drain(1)
        self.assertEqual(len(drained), 1)
        self.assertEqual(buffer.requeue_front(drained), 1)
        self.assertEqual(buffer.stats().queued_points, 2)

    def test_runtime_scrape_enters_buffer(self):
        collector = FakeCollector()
        collector.samples = (self.sample(),)
        runtime = OpenMetricsRuntime(collector)
        runtime.replace_targets((self.target(),), now=10)
        batches = runtime.tick(now=10, wall_time=1000)
        self.assertEqual(len(batches), 1)
        point = runtime.buffer.drain(1)[0]
        self.assertEqual(point.name, "demo.temperature")
        self.assertEqual(point.timestamp, 1000)

    def test_background_worker_hot_reload_and_stop(self):
        collector = FakeCollector()
        collector.samples = (self.sample(),)
        worker = OpenMetricsRuntimeWorker(collector, max_idle_wait_s=0.02)
        worker.replace_targets((self.target(),))
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and worker.buffer.stats().queued_points == 0:
            time.sleep(0.01)
        self.assertGreaterEqual(worker.buffer.stats().queued_points, 1)
        worker.replace_targets(())
        time.sleep(0.04)
        self.assertTrue(worker.stop(timeout_s=1.0))
        self.assertFalse(worker.is_running())


if __name__ == "__main__":
    unittest.main()
