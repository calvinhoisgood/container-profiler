"""Normalize headless Agent health into queryable/forwardable self metrics."""
from __future__ import annotations

from math import isfinite
from typing import Any

from .custom_metrics import CustomMetricPoint


def _get(value: Any, *path: str, default=None):
    current = value
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            current = getattr(current, key, default)
    return current


def normalize_agent_self_metrics(
    snapshot: Any,
    *,
    timestamp: float,
    hostname: str,
) -> tuple[CustomMetricPoint, ...]:
    """Flatten stable Agent health counters without depending on concrete classes."""
    host = hostname.strip() or "unknown"
    common = dict(
        timestamp=float(timestamp),
        tags=(f"host:{host}",),
        metric_type="gauge",
        source="agent",
        target_key="agent-self",
    )
    points: list[CustomMetricPoint] = []

    def add(name: str, value: Any, *extra_tags: str) -> None:
        if value is None:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if not isfinite(numeric):
            return
        payload = dict(common)
        payload["tags"] = tuple((*common["tags"], *extra_tags))
        points.append(CustomMetricPoint(name=name, value=numeric, **payload))

    add("container_profiler.agent.ticks", _get(snapshot, "ticks"))
    for field, subsystem in (
        ("host_samples_persisted", "host"),
        ("system_points_persisted", "system"),
        ("container_points_persisted", "container"),
        ("statsd_points_persisted", "dogstatsd"),
        ("openmetrics_points_persisted", "openmetrics"),
        ("alert_events_persisted", "alerts"),
    ):
        add(
            "container_profiler.agent.persisted",
            _get(snapshot, field),
            f"subsystem:{subsystem}",
        )
    for field, subsystem in (
        ("host_storage_failures", "host"),
        ("system_storage_failures", "system"),
        ("container_storage_failures", "container"),
        ("statsd_storage_failures", "dogstatsd"),
        ("openmetrics_storage_failures", "openmetrics"),
        ("self_storage_failures", "self"),
        ("retention_failures", "retention"),
    ):
        add(
            "container_profiler.agent.storage_failures",
            _get(snapshot, field, default=0),
            f"subsystem:{subsystem}",
        )
    add(
        "container_profiler.alert.failures",
        _get(snapshot, "alert_failures", default=0),
    )
    add(
        "container_profiler.alert.audit_queue",
        _get(snapshot, "alert_events_queued", default=0),
    )
    add(
        "container_profiler.alert.audit_dropped",
        _get(snapshot, "alert_events_dropped", default=0),
    )

    host_runtime = _get(snapshot, "host")
    if host_runtime is not None:
        for field, metric in (
            ("samples_collected", "container_profiler.host.samples_collected"),
            ("failed_samples", "container_profiler.host.failed_samples"),
            ("queued_samples", "container_profiler.host.queued_samples"),
            ("dropped_samples", "container_profiler.host.dropped_samples"),
        ):
            add(metric, _get(host_runtime, field))

    system = _get(snapshot, "system")
    if system is not None:
        for field, metric in (
            ("collections", "container_profiler.system.collections"),
            ("failed_collections", "container_profiler.system.failed_collections"),
            ("partial_collections", "container_profiler.system.partial_collections"),
        ):
            add(metric, _get(system, field))
        add(
            "container_profiler.system.buffer_dropped_points",
            _get(system, "buffer", "dropped_points"),
        )
        for field, check in (
            ("network_error", "network"),
            ("disk_error", "disk"),
            ("filesystem_error", "filesystem"),
            ("process_error", "process"),
        ):
            add(
                "container_profiler.system.check_error",
                1 if _get(system, field) else 0,
                f"check:{check}",
            )

    statsd = _get(snapshot, "statsd")
    add("container_profiler.dogstatsd.parse_errors", _get(statsd, "parse_errors"))
    add("container_profiler.dogstatsd.dropped_series", _get(statsd, "dropped_series"))
    add(
        "container_profiler.dogstatsd.dropped_histogram_values",
        _get(statsd, "dropped_histogram_values"),
    )
    add(
        "container_profiler.dogstatsd.dropped_set_values",
        _get(statsd, "dropped_set_values"),
    )
    add(
        "container_profiler.dogstatsd.buffer_dropped_points",
        _get(statsd, "buffer", "dropped_points"),
    )

    openmetrics = _get(snapshot, "openmetrics")
    add(
        "container_profiler.openmetrics.discovery_failures",
        _get(openmetrics, "refresh_failures"),
    )
    add("container_profiler.openmetrics.targets", _get(openmetrics, "target_count"))
    add(
        "container_profiler.openmetrics.buffer_dropped_points",
        _get(openmetrics, "worker", "buffer", "dropped_points"),
    )

    containers = _get(snapshot, "containers")
    add(
        "container_profiler.container.discovery_failures",
        _get(containers, "discovery_failures"),
    )
    add(
        "container_profiler.container.sample_failures",
        _get(containers, "sample_failures"),
    )
    add("container_profiler.container.seen", _get(containers, "containers_seen"))
    add(
        "container_profiler.container.buffer_dropped_points",
        _get(containers, "buffer", "dropped_points"),
    )

    metric_alerts = _get(snapshot, "metric_alerts")
    for field, metric in (
        ("rule_count", "container_profiler.alert.rules"),
        ("tracked_series", "container_profiler.alert.tracked_series"),
        ("event_count", "container_profiler.alert.recent_events"),
        ("evicted_series", "container_profiler.alert.evicted_series"),
        ("invalid_points", "container_profiler.alert.invalid_points"),
    ):
        add(metric, _get(metric_alerts, field))
    if metric_alerts is not None:
        add(
            "container_profiler.alert.config_error",
            1 if _get(metric_alerts, "last_config_error") else 0,
        )

    forwarding = _get(snapshot, "forwarding")
    add(
        "container_profiler.forwarding.reload_failures",
        _get(forwarding, "reload_failures"),
    )
    add(
        "container_profiler.forwarding.enqueue_failures",
        _get(forwarding, "enqueue_failures"),
    )
    add(
        "container_profiler.forwarding.points_enqueued",
        _get(forwarding, "points_enqueued"),
    )
    add(
        "container_profiler.forwarding.points_dropped",
        _get(forwarding, "points_dropped"),
    )
    for field, metric in (
        ("queued_items", "container_profiler.forwarding.queued_items"),
        ("queued_bytes", "container_profiler.forwarding.queued_bytes"),
        ("dropped_items", "container_profiler.forwarding.dropped_items"),
        ("failed_attempts", "container_profiler.forwarding.failed_attempts"),
    ):
        add(metric, _get(forwarding, "queue", field))

    return tuple(points)