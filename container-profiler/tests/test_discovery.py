"""Hardware-free tests for container metadata and event discovery."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.discovery import (
    DockerEventWatcher,
    ReconnectBackoff,
    extract_container_metadata,
    normalize_container_event,
)
from profiler.core.docker_monitor import DockerMonitor


class ClosableEvents:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class FakeContainers:
    def __init__(self):
        self.item = SimpleNamespace(
            id="0123456789abcdef",
            short_id="0123456789ab",
            name="api",
            status="running",
            image=SimpleNamespace(tags=["example/api:1.2.3"]),
            attrs={
                "Created": "2026-01-01T00:00:00Z",
                "Config": {
                    "Image": "example/api:1.2.3",
                    "Labels": {
                        "com.datadoghq.tags.env": "prod",
                        "com.datadoghq.tags.service": "orders",
                        "com.datadoghq.tags.version": "1.2.3",
                        "com.docker.compose.project": "shop",
                        "com.datadoghq.ad.tags": '["team:payments", "tier:api"]',
                    },
                },
            },
        )

    def list(self, all=True):
        return [self.item]


class FakeClient:
    def __init__(self, events=None):
        self.containers = FakeContainers()
        self.stream = ClosableEvents(events or [])
        self.event_calls = []

    def ping(self):
        return True

    def events(self, **kwargs):
        self.event_calls.append(kwargs)
        return self.stream


class DiscoveryTests(unittest.TestCase):
    def test_extracts_unified_compose_and_ad_tags(self):
        metadata = extract_container_metadata(
            {
                "com.datadoghq.tags.env": "staging",
                "com.datadoghq.tags.service": "checkout",
                "com.datadoghq.tags.version": "sha-123",
                "com.docker.compose.service": "web",
                "com.datadoghq.ad.tags": '{"owner":"core", "region":"sg"}',
            },
            container_name="checkout-1",
            image="checkout:sha-123",
        )
        self.assertEqual(metadata.env, "staging")
        self.assertEqual(metadata.service, "checkout")
        self.assertEqual(metadata.version, "sha-123")
        self.assertIn("owner:core", metadata.tags)
        self.assertIn("compose_service:web", metadata.tags)
        self.assertIn("container_name:checkout-1", metadata.tags)

    def test_malformed_ad_tags_do_not_break_discovery(self):
        metadata = extract_container_metadata({"com.datadoghq.ad.tags": "not-json"})
        self.assertEqual(metadata.tags, ())

    def test_normalizes_container_event_and_ignores_other_types(self):
        event = normalize_container_event({
            "Type": "container",
            "Action": "start",
            "timeNano": 2_500_000_000,
            "Actor": {"ID": "abc", "Attributes": {"name": "api", "image": "api:v1"}},
        })
        self.assertIsNotNone(event)
        self.assertEqual(event.timestamp, 2.5)
        self.assertEqual(event.container_id, "abc")
        self.assertEqual(event.name, "api")
        self.assertIsNone(normalize_container_event({"Type": "network", "Action": "create", "id": "n1"}))

    def test_watcher_delivers_normalized_events(self):
        stream = ClosableEvents([
            {"Type": "container", "Action": "start", "id": "a", "time": 1},
            {"Type": "network", "Action": "create", "id": "n", "time": 2},
            {"Type": "container", "Action": "die", "id": "a", "time": 3},
        ])
        watcher = DockerEventWatcher(lambda: stream)
        seen = []
        watcher.run(seen.append)
        self.assertEqual([event.action for event in seen], ["start", "die"])
        self.assertIsNone(watcher.last_error)

    def test_docker_monitor_enriches_list_and_requests_container_events(self):
        client = FakeClient([{"Type": "container", "Action": "start", "id": "abc", "time": 1}])
        monitor = DockerMonitor(client=client)
        containers = monitor.list_containers()
        self.assertEqual(containers[0].service, "orders")
        self.assertEqual(containers[0].env, "prod")
        self.assertIn("team:payments", containers[0].tags)

        watcher = monitor.create_event_watcher()
        self.assertIsNotNone(watcher)
        seen = []
        watcher.run(seen.append)
        self.assertEqual(seen[0].action, "start")
        self.assertEqual(client.event_calls, [{"decode": True, "filters": {"type": "container"}}])

    def test_reconnect_backoff_is_bounded_and_resettable(self):
        backoff = ReconnectBackoff(initial_s=0.25, maximum_s=1.0, factor=2.0)
        self.assertEqual(
            [backoff.next_delay() for _ in range(5)],
            [0.25, 0.5, 1.0, 1.0, 1.0],
        )
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 0.25)

    def test_reconnect_backoff_rejects_invalid_policy(self):
        for kwargs in (
            {"initial_s": 0},
            {"initial_s": 2, "maximum_s": 1},
            {"factor": 1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ReconnectBackoff(**kwargs)


if __name__ == "__main__":
    unittest.main()
