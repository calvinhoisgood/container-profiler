"""OpenMetrics Autodiscovery, target resolution and bounded runtime scheduling.

This layer converts Datadog-compatible Docker labels into safe normalized
OpenMetrics targets, resolves Docker inspect network metadata, and provides a
background worker with bounded custom-metric buffering. It is independent from
Qt so the same runtime can later move into a Windows service/agent process.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .container_context import ContainerTemplateContext
from .custom_metrics import (
    BoundedMetricBuffer,
    CustomMetricPoint,
    MetricBufferStats,
    normalize_openmetrics_samples,
)

AD_CHECKS_LABEL = "com.datadoghq.ad.checks"
AD_CHECK_NAMES_LABEL = "com.datadoghq.ad.check_names"
AD_INIT_CONFIGS_LABEL = "com.datadoghq.ad.init_configs"
AD_INSTANCES_LABEL = "com.datadoghq.ad.instances"
DEFAULT_INTERVAL_S = 15.0
DEFAULT_MAX_RETURNED_METRICS = 2000
MAX_TARGETS_PER_CONTAINER = 16
MAX_TOTAL_TARGETS = 512
MAX_DISCOVERY_ERRORS = 256
MAX_METRIC_PATTERNS = 256
MAX_TAGS = 128
_TOKEN_RE = re.compile(r"%%([^%]+)%%")
_HOST_NETWORK_TOKEN_RE = re.compile(r"^host_(.+)$")
_PORT_INDEX_TOKEN_RE = re.compile(r"^port_(\d+)$")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_PENDING_UNSET = object()


class OpenMetricsDiscoveryError(ValueError):
    """Raised when an Autodiscovery instance is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class OpenMetricsTargetTemplate:
    key: str
    endpoint_template: str
    namespace: str
    metric_patterns: tuple[str, ...]
    metric_renames: tuple[tuple[str, str], ...] = ()
    interval_s: float = DEFAULT_INTERVAL_S
    max_samples: int = DEFAULT_MAX_RETURNED_METRICS
    tags: tuple[str, ...] = ()
    container_id: str | None = None

    def resolve(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        context: ContainerTemplateContext | None = None,
        allow_external: bool = False,
    ) -> "OpenMetricsTarget":
        if context is None:
            context = ContainerTemplateContext(
                primary_host=host,
                network_hosts=(("default", host),) if host else (),
                ports=(int(port),) if port is not None else (),
            )
        url = resolve_endpoint_template(
            self.endpoint_template,
            context=context,
            port_override=port,
            allow_external=allow_external,
        )
        return OpenMetricsTarget(
            key=self.key,
            url=url,
            namespace=self.namespace,
            metric_patterns=self.metric_patterns,
            metric_renames=self.metric_renames,
            interval_s=self.interval_s,
            max_samples=self.max_samples,
            tags=self.tags,
            container_id=self.container_id,
        )


@dataclass(frozen=True, slots=True)
class OpenMetricsTarget:
    key: str
    url: str
    namespace: str
    metric_patterns: tuple[str, ...]
    metric_renames: tuple[tuple[str, str], ...] = ()
    interval_s: float = DEFAULT_INTERVAL_S
    max_samples: int = DEFAULT_MAX_RETURNED_METRICS
    tags: tuple[str, ...] = ()
    container_id: str | None = None


@dataclass(frozen=True, slots=True)
class OpenMetricsDiscoveryResult:
    targets: tuple[OpenMetricsTargetTemplate, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpenMetricsResolvedSet:
    targets: tuple[OpenMetricsTarget, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpenMetricsTargetHealth:
    key: str
    next_due: float
    successful_scrapes: int = 0
    failed_scrapes: int = 0
    last_success_at: float | None = None
    last_error: str | None = None
    last_sample_count: int | None = None
    last_payload_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class OpenMetricsScrapeBatch:
    target: OpenMetricsTarget
    result: Any


@dataclass(frozen=True, slots=True)
class OpenMetricsWorkerSnapshot:
    running: bool
    ticks: int
    target_health: tuple[OpenMetricsTargetHealth, ...]
    buffer: MetricBufferStats
    last_error: str | None = None


def _load_json(value: str, *, label: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise OpenMetricsDiscoveryError(f"{label}: invalid JSON") from exc


def _decode_nested_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


def _parse_metrics(raw: Any) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    if not isinstance(raw, list) or not raw:
        raise OpenMetricsDiscoveryError("openmetrics instance requires a non-empty metrics list")
    if len(raw) > MAX_METRIC_PATTERNS:
        raise OpenMetricsDiscoveryError(
            f"openmetrics metrics list exceeds {MAX_METRIC_PATTERNS} entries"
        )

    patterns: list[str] = []
    renames: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            pattern = item
        elif isinstance(item, dict) and len(item) == 1:
            pattern, destination = next(iter(item.items()))
            if not isinstance(pattern, str):
                raise OpenMetricsDiscoveryError("metric mapping key must be a string")
            if isinstance(destination, str):
                if not destination or not _METRIC_NAME.fullmatch(destination):
                    raise OpenMetricsDiscoveryError("metric alias must be a valid metric name")
                renames.append((pattern, destination))
            elif destination is not None and not isinstance(destination, dict):
                raise OpenMetricsDiscoveryError("unsupported metric mapping value")
        else:
            raise OpenMetricsDiscoveryError("metrics entries must be strings or one-key mappings")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise OpenMetricsDiscoveryError(f"invalid metric regex {pattern!r}: {exc}") from exc
        patterns.append(pattern)
    return tuple(patterns), tuple(renames)


def _parse_tags(raw: Any, inherited: Iterable[str]) -> tuple[str, ...]:
    tags = list(inherited)
    if raw is not None:
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise OpenMetricsDiscoveryError("tags must be a list of strings")
        tags.extend(raw)
    unique = tuple(dict.fromkeys(tag for tag in tags if tag))
    if len(unique) > MAX_TAGS:
        raise OpenMetricsDiscoveryError(f"tag count exceeds {MAX_TAGS}")
    return unique


def _parse_instance(
    instance: Any,
    *,
    key: str,
    container_id: str,
    inherited_tags: Iterable[str],
) -> OpenMetricsTargetTemplate:
    instance = _decode_nested_json(instance)
    if not isinstance(instance, Mapping):
        raise OpenMetricsDiscoveryError("openmetrics instance must be an object")

    endpoint = instance.get("openmetrics_endpoint") or instance.get("prometheus_url")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise OpenMetricsDiscoveryError("openmetrics_endpoint is required")

    namespace = instance.get("namespace", "")
    if not isinstance(namespace, str):
        raise OpenMetricsDiscoveryError("namespace must be a string")
    clean_namespace = namespace.rstrip(".")
    if clean_namespace and not _METRIC_NAME.fullmatch(clean_namespace):
        raise OpenMetricsDiscoveryError("invalid namespace")

    patterns, renames = _parse_metrics(instance.get("metrics"))

    interval_raw = instance.get("min_collection_interval", DEFAULT_INTERVAL_S)
    try:
        interval_s = float(interval_raw)
    except (TypeError, ValueError) as exc:
        raise OpenMetricsDiscoveryError("min_collection_interval must be numeric") from exc
    if not 1.0 <= interval_s <= 3600.0:
        raise OpenMetricsDiscoveryError("min_collection_interval must be between 1 and 3600 seconds")

    max_raw = instance.get("max_returned_metrics", DEFAULT_MAX_RETURNED_METRICS)
    if isinstance(max_raw, bool):
        raise OpenMetricsDiscoveryError("max_returned_metrics must be an integer")
    try:
        max_samples = int(max_raw)
    except (TypeError, ValueError) as exc:
        raise OpenMetricsDiscoveryError("max_returned_metrics must be an integer") from exc
    if max_samples <= 0 or max_samples > 10_000:
        raise OpenMetricsDiscoveryError("max_returned_metrics must be between 1 and 10000")

    return OpenMetricsTargetTemplate(
        key=key,
        endpoint_template=endpoint.strip(),
        namespace=clean_namespace,
        metric_patterns=patterns,
        metric_renames=renames,
        interval_s=interval_s,
        max_samples=max_samples,
        tags=_parse_tags(instance.get("tags"), inherited_tags),
        container_id=container_id,
    )


def _v2_instances(labels: Mapping[str, str]) -> list[Any]:
    raw = labels.get(AD_CHECKS_LABEL)
    if not raw:
        return []
    payload = _load_json(raw, label=AD_CHECKS_LABEL)
    if not isinstance(payload, Mapping):
        raise OpenMetricsDiscoveryError(f"{AD_CHECKS_LABEL}: root must be an object")
    config = payload.get("openmetrics")
    if config is None:
        return []
    if not isinstance(config, Mapping):
        raise OpenMetricsDiscoveryError("openmetrics check config must be an object")
    instances = config.get("instances", [])
    if not isinstance(instances, list):
        raise OpenMetricsDiscoveryError("openmetrics instances must be a list")
    return instances


def _legacy_instances(labels: Mapping[str, str]) -> list[Any]:
    names_raw = labels.get(AD_CHECK_NAMES_LABEL)
    instances_raw = labels.get(AD_INSTANCES_LABEL)
    if not names_raw and not instances_raw:
        return []
    if not names_raw or not instances_raw:
        raise OpenMetricsDiscoveryError("legacy Autodiscovery requires check_names and instances")
    names = _load_json(names_raw, label=AD_CHECK_NAMES_LABEL)
    instances = _load_json(instances_raw, label=AD_INSTANCES_LABEL)
    if not isinstance(names, list) or not isinstance(instances, list):
        raise OpenMetricsDiscoveryError("legacy check_names and instances must be lists")
    if len(names) != len(instances):
        raise OpenMetricsDiscoveryError("legacy check_names and instances length mismatch")
    return [instance for name, instance in zip(names, instances) if name == "openmetrics"]


def discover_openmetrics_targets(
    labels: Mapping[str, Any] | None,
    *,
    container_key: str,
    inherited_tags: Iterable[str] = (),
) -> OpenMetricsDiscoveryResult:
    """Parse v2 or legacy Datadog Docker labels into bounded target templates."""
    normalized = {
        str(key): str(value)
        for key, value in (labels or {}).items()
        if key is not None and value is not None
    }
    errors: list[str] = []
    try:
        instances = _v2_instances(normalized)
        if not instances:
            instances = _legacy_instances(normalized)
    except OpenMetricsDiscoveryError as exc:
        return OpenMetricsDiscoveryResult((), (str(exc),))

    if len(instances) > MAX_TARGETS_PER_CONTAINER:
        return OpenMetricsDiscoveryResult(
            (), (f"openmetrics target count exceeds {MAX_TARGETS_PER_CONTAINER}",)
        )

    targets: list[OpenMetricsTargetTemplate] = []
    for index, instance in enumerate(instances):
        try:
            targets.append(
                _parse_instance(
                    instance,
                    key=f"{container_key}:openmetrics:{index}",
                    container_id=container_key,
                    inherited_tags=inherited_tags,
                )
            )
        except OpenMetricsDiscoveryError as exc:
            errors.append(f"instance {index}: {exc}")
    return OpenMetricsDiscoveryResult(tuple(targets), tuple(errors))


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def resolve_endpoint_template(
    endpoint_template: str,
    *,
    host: str | None = None,
    port: int | None = None,
    context: ContainerTemplateContext | None = None,
    port_override: int | None = None,
    allow_external: bool = False,
) -> str:
    """Resolve Datadog Docker template variables with an SSRF-safe default.

    Supported variables are ``%%host%%``, ``%%host_<NETWORK>%%``, ``%%port%%``,
    numeric ``%%port_N%%``, ``%%hostname%%`` and ``%%pid%%``. Datadog defines
    ``%%port%%`` as the highest exposed port and indexed ports in ascending
    numeric order. A missing named network falls back to ``%%host%%``.

    Label-controlled checks are untrusted input. Unless ``allow_external`` is
    explicitly enabled, the final URL hostname must be one of the IP addresses
    Docker discovered for this container. This prevents labels from turning the
    desktop agent into an arbitrary HTTP/metadata-network probe.
    """
    if context is None:
        chosen_port = port_override if port_override is not None else port
        context = ContainerTemplateContext(
            primary_host=host,
            network_hosts=(("default", host),) if host else (),
            ports=(int(chosen_port),) if chosen_port is not None else (),
        )
    elif port_override is None and port is not None:
        port_override = port

    ports = tuple(sorted(set(int(value) for value in context.ports if 1 <= int(value) <= 65535)))
    if port_override is not None:
        if not 1 <= int(port_override) <= 65535:
            raise OpenMetricsDiscoveryError("container port must be between 1 and 65535")
        default_port = int(port_override)
    else:
        default_port = ports[-1] if ports else None

    allowed_hosts = {value for _, value in context.network_hosts if value}
    if context.primary_host:
        allowed_hosts.add(context.primary_host)

    def render_token(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "host":
            value = context.primary_host
            if not value:
                raise OpenMetricsDiscoveryError(
                    "endpoint requires %%host%% but no container IP was discovered"
                )
            return _url_host(value)

        network_match = _HOST_NETWORK_TOKEN_RE.fullmatch(token)
        if network_match:
            network_name = network_match.group(1)
            value = context.host_for_network(network_name)
            if not value:
                raise OpenMetricsDiscoveryError(
                    f"endpoint requires %%host_{network_name}%% but no container IP was discovered"
                )
            return _url_host(value)

        if token == "port":
            if default_port is None:
                raise OpenMetricsDiscoveryError(
                    "endpoint requires %%port%% but no exposed port was discovered"
                )
            return str(default_port)

        port_match = _PORT_INDEX_TOKEN_RE.fullmatch(token)
        if port_match:
            index = int(port_match.group(1))
            if index >= len(ports):
                raise OpenMetricsDiscoveryError(
                    f"endpoint port index {index} is unavailable"
                )
            return str(ports[index])

        if token == "hostname":
            if not context.hostname:
                raise OpenMetricsDiscoveryError(
                    "endpoint requires %%hostname%% but no container hostname was discovered"
                )
            return context.hostname

        if token == "pid":
            if context.pid is None:
                raise OpenMetricsDiscoveryError(
                    "endpoint requires %%pid%% but no container pid was discovered"
                )
            return str(context.pid)

        raise OpenMetricsDiscoveryError(f"unsupported template variable: %%{token}%%")

    rendered = _TOKEN_RE.sub(render_token, endpoint_template)
    parsed = urlsplit(rendered)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OpenMetricsDiscoveryError("endpoint must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise OpenMetricsDiscoveryError("endpoint credentials are not allowed in labels")
    if parsed.fragment:
        raise OpenMetricsDiscoveryError("endpoint fragments are not supported")
    if not allow_external and parsed.hostname not in allowed_hosts:
        raise OpenMetricsDiscoveryError(
            "autodiscovery endpoint must resolve to a discovered container IP"
        )
    return rendered


def resolve_openmetrics_for_containers(
    containers: Iterable[Any],
) -> OpenMetricsResolvedSet:
    """Discover and resolve checks for running ``ContainerInfo`` snapshots."""
    targets: list[OpenMetricsTarget] = []
    errors: list[str] = []

    for container in containers:
        if str(getattr(container, "status", "")) != "running":
            continue
        container_id = str(getattr(container, "id", ""))
        labels = dict(getattr(container, "labels", ()) or ())
        discovery = discover_openmetrics_targets(
            labels,
            container_key=container_id,
            inherited_tags=tuple(getattr(container, "tags", ()) or ()),
        )
        for error in discovery.errors:
            if len(errors) < MAX_DISCOVERY_ERRORS:
                errors.append(f"{container_id[:12]}: {error}")

        context = ContainerTemplateContext(
            primary_host=getattr(container, "primary_host", None),
            network_hosts=tuple(getattr(container, "network_hosts", ()) or ()),
            ports=tuple(getattr(container, "exposed_ports", ()) or ()),
            hostname=getattr(container, "hostname", None),
            pid=getattr(container, "pid", None),
        )
        for template in discovery.targets:
            if len(targets) >= MAX_TOTAL_TARGETS:
                if len(errors) < MAX_DISCOVERY_ERRORS:
                    errors.append(f"global OpenMetrics target limit {MAX_TOTAL_TARGETS} reached")
                return OpenMetricsResolvedSet(tuple(targets), tuple(errors))
            try:
                targets.append(template.resolve(context=context))
            except OpenMetricsDiscoveryError as exc:
                if len(errors) < MAX_DISCOVERY_ERRORS:
                    errors.append(f"{container_id[:12]}: {template.key}: {exc}")

    return OpenMetricsResolvedSet(tuple(targets), tuple(errors))


class OpenMetricsCheckScheduler:
    """Deterministic bounded scheduler for dynamically discovered targets."""

    def __init__(self, collector: Any) -> None:
        self.collector = collector
        self._targets: dict[str, OpenMetricsTarget] = {}
        self._health: dict[str, OpenMetricsTargetHealth] = {}

    def replace_targets(self, targets: Iterable[OpenMetricsTarget], *, now: float) -> None:
        incoming: dict[str, OpenMetricsTarget] = {}
        for target in targets:
            if target.key in incoming:
                raise ValueError(f"duplicate OpenMetrics target key: {target.key}")
            incoming[target.key] = target

        health: dict[str, OpenMetricsTargetHealth] = {}
        for key, target in incoming.items():
            previous_target = self._targets.get(key)
            previous_health = self._health.get(key)
            if previous_target == target and previous_health is not None:
                health[key] = previous_health
            else:
                health[key] = OpenMetricsTargetHealth(key=key, next_due=float(now))
        self._targets = incoming
        self._health = health

    def snapshot(self) -> tuple[OpenMetricsTargetHealth, ...]:
        return tuple(self._health[key] for key in sorted(self._health))

    def next_due(self) -> float | None:
        if not self._health:
            return None
        return min(item.next_due for item in self._health.values())

    def run_due(self, *, now: float) -> tuple[OpenMetricsScrapeBatch, ...]:
        batches: list[OpenMetricsScrapeBatch] = []
        for key in sorted(self._targets):
            target = self._targets[key]
            health = self._health[key]
            if now < health.next_due:
                continue
            next_due = float(now) + target.interval_s
            try:
                # Keep exposition names raw here. Namespace and aliases are
                # applied by the normalized custom-metric pipeline afterwards.
                result = self.collector.scrape(
                    target.url,
                    metric_patterns=target.metric_patterns,
                    namespace="",
                    max_samples=target.max_samples,
                )
            except Exception as exc:
                self._health[key] = replace(
                    health,
                    next_due=next_due,
                    failed_scrapes=health.failed_scrapes + 1,
                    last_error=str(exc),
                )
                continue

            samples = getattr(result, "samples", ())
            payload_bytes = getattr(result, "payload_bytes", None)
            self._health[key] = replace(
                health,
                next_due=next_due,
                successful_scrapes=health.successful_scrapes + 1,
                last_success_at=float(now),
                last_error=None,
                last_sample_count=len(samples),
                last_payload_bytes=payload_bytes,
            )
            batches.append(OpenMetricsScrapeBatch(target=target, result=result))
        return tuple(batches)


class OpenMetricsRuntime:
    """Deterministic scheduler + normalization + bounded buffer composition."""

    def __init__(
        self,
        collector: Any,
        *,
        buffer: BoundedMetricBuffer | None = None,
    ) -> None:
        self.scheduler = OpenMetricsCheckScheduler(collector)
        self.buffer = buffer or BoundedMetricBuffer()
        self.ticks = 0

    def replace_targets(self, targets: Iterable[OpenMetricsTarget], *, now: float) -> None:
        self.scheduler.replace_targets(targets, now=now)

    def tick(self, *, now: float, wall_time: float) -> tuple[OpenMetricsScrapeBatch, ...]:
        batches = self.scheduler.run_due(now=now)
        for batch in batches:
            self.buffer.append_many(
                normalize_openmetrics_samples(
                    batch.target,
                    batch.result,
                    collected_at=wall_time,
                )
            )
        self.ticks += 1
        return batches


class OpenMetricsRuntimeWorker:
    """Background worker for scrape I/O with cooperative target hot-reload.

    Docker/HTTP calls never execute on the Qt main thread. Target updates are
    coalesced to the newest snapshot. Collected points enter a bounded queue
    that consumers can drain and, on a failed delivery, requeue at the front.
    """

    def __init__(
        self,
        collector: Any,
        *,
        buffer: BoundedMetricBuffer | None = None,
        monotonic_clock=time.monotonic,
        wall_clock=time.time,
        max_idle_wait_s: float = 1.0,
    ) -> None:
        if max_idle_wait_s <= 0:
            raise ValueError("max_idle_wait_s must be positive")
        self.runtime = OpenMetricsRuntime(collector, buffer=buffer)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._max_idle_wait_s = float(max_idle_wait_s)
        self._condition = threading.Condition()
        self._pending_targets: object | tuple[OpenMetricsTarget, ...] = _PENDING_UNSET
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._snapshot_lock = threading.Lock()
        self._last_health: tuple[OpenMetricsTargetHealth, ...] = ()
        self._last_error: str | None = None

    @property
    def buffer(self) -> BoundedMetricBuffer:
        return self.runtime.buffer

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.is_running():
                return
            self._stop_requested = False
            thread = threading.Thread(
                target=self._run,
                name="openmetrics-runtime",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def replace_targets(self, targets: Iterable[OpenMetricsTarget]) -> None:
        values = tuple(targets)
        keys = [target.key for target in values]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate OpenMetrics target key")
        with self._condition:
            self._pending_targets = values
            self._condition.notify_all()

    def drain_points(self, limit: int = 1000) -> tuple[CustomMetricPoint, ...]:
        return self.buffer.drain(limit)

    def requeue_points(self, points: Iterable[CustomMetricPoint]) -> int:
        return self.buffer.requeue_front(points)

    def snapshot(self) -> OpenMetricsWorkerSnapshot:
        with self._snapshot_lock:
            health = self._last_health
            last_error = self._last_error
        return OpenMetricsWorkerSnapshot(
            running=self.is_running(),
            ticks=self.runtime.ticks,
            target_health=health,
            buffer=self.buffer.stats(),
            last_error=last_error,
        )

    def stop(self, timeout_s: float = 6.0) -> bool:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout_s))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _take_pending_targets(self) -> object | tuple[OpenMetricsTarget, ...]:
        with self._condition:
            pending = self._pending_targets
            self._pending_targets = _PENDING_UNSET
            return pending

    def _set_snapshot_error(self, error: str | None) -> None:
        with self._snapshot_lock:
            self._last_health = self.runtime.scheduler.snapshot()
            self._last_error = error

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stop_requested:
                    break

            pending = self._take_pending_targets()
            if pending is not _PENDING_UNSET:
                self.runtime.replace_targets(
                    pending,
                    now=float(self._monotonic_clock()),
                )

            try:
                now = float(self._monotonic_clock())
                self.runtime.tick(now=now, wall_time=float(self._wall_clock()))
                self._set_snapshot_error(None)
            except Exception as exc:
                # A programming/data-normalization failure must degrade the
                # custom-metric subsystem rather than killing the whole app.
                self._set_snapshot_error(str(exc))

            next_due = self.runtime.scheduler.next_due()
            if next_due is None:
                wait_s = self._max_idle_wait_s
            else:
                wait_s = min(
                    self._max_idle_wait_s,
                    max(0.0, next_due - float(self._monotonic_clock())),
                )
                if wait_s == 0:
                    # Avoid a CPU spin when a custom clock does not advance.
                    wait_s = min(0.01, self._max_idle_wait_s)

            with self._condition:
                if self._stop_requested:
                    break
                if self._pending_targets is _PENDING_UNSET:
                    self._condition.wait(wait_s)

        self._set_snapshot_error(self._last_error)
