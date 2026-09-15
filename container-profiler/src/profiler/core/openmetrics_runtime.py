"""OpenMetrics Autodiscovery config and bounded check scheduling.

This layer converts Datadog-compatible Docker labels into safe, normalized
OpenMetrics targets and owns deterministic scheduling/health accounting. It is
independent from Docker and Qt so it can be used by the desktop app or a future
background agent process.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

AD_CHECKS_LABEL = "com.datadoghq.ad.checks"
AD_CHECK_NAMES_LABEL = "com.datadoghq.ad.check_names"
AD_INIT_CONFIGS_LABEL = "com.datadoghq.ad.init_configs"
AD_INSTANCES_LABEL = "com.datadoghq.ad.instances"
DEFAULT_INTERVAL_S = 15.0
DEFAULT_MAX_RETURNED_METRICS = 2000
MAX_TARGETS_PER_CONTAINER = 16
MAX_METRIC_PATTERNS = 256
MAX_TAGS = 128
_TOKEN_RE = re.compile(r"%%[^%]+%%")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


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

    def resolve(
        self,
        *,
        host: str,
        port: int | None = None,
        allow_external: bool = False,
    ) -> "OpenMetricsTarget":
        url = resolve_endpoint_template(
            self.endpoint_template,
            host=host,
            port=port,
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


@dataclass(frozen=True, slots=True)
class OpenMetricsDiscoveryResult:
    targets: tuple[OpenMetricsTargetTemplate, ...]
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
    if max_samples <= 0 or max_samples > 10000:
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
    """Parse v2 or legacy Datadog Docker labels into bounded target templates.

    V2 ``com.datadoghq.ad.checks`` takes precedence when it contains an
    OpenMetrics check. Invalid instances are isolated and reported rather than
    preventing other valid endpoints from being scheduled.
    """
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
                    inherited_tags=inherited_tags,
                )
            )
        except OpenMetricsDiscoveryError as exc:
            errors.append(f"instance {index}: {exc}")
    return OpenMetricsDiscoveryResult(tuple(targets), tuple(errors))


def resolve_endpoint_template(
    endpoint_template: str,
    *,
    host: str,
    port: int | None = None,
    allow_external: bool = False,
) -> str:
    """Resolve Datadog ``%%host%%``/``%%port%%`` with an SSRF-safe default.

    Label-controlled Autodiscovery is untrusted input. Unless explicitly
    opted out, the URL hostname must be ``%%host%%`` so a container cannot make
    the desktop agent probe arbitrary hosts or cloud metadata services.
    """
    if not host or any(char.isspace() for char in host):
        raise OpenMetricsDiscoveryError("container host is empty or invalid")
    if port is not None and not 1 <= int(port) <= 65535:
        raise OpenMetricsDiscoveryError("container port must be between 1 and 65535")

    probe = endpoint_template.replace("%%host%%", "container.invalid")
    probe = probe.replace("%%port%%", "65535")
    parsed_probe = urlsplit(probe)
    if parsed_probe.scheme not in {"http", "https"} or not parsed_probe.hostname:
        raise OpenMetricsDiscoveryError("endpoint must be an absolute http(s) URL")
    if parsed_probe.username is not None or parsed_probe.password is not None:
        raise OpenMetricsDiscoveryError("endpoint credentials are not allowed in labels")
    if parsed_probe.fragment:
        raise OpenMetricsDiscoveryError("endpoint fragments are not supported")
    if not allow_external and parsed_probe.hostname != "container.invalid":
        raise OpenMetricsDiscoveryError("autodiscovery endpoint must use %%host%%")
    if "%%port%%" in endpoint_template and port is None:
        raise OpenMetricsDiscoveryError("endpoint requires %%port%% but no port was supplied")

    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    rendered = endpoint_template.replace("%%host%%", rendered_host)
    if port is not None:
        rendered = rendered.replace("%%port%%", str(int(port)))
    token = _TOKEN_RE.search(rendered)
    if token:
        raise OpenMetricsDiscoveryError(f"unsupported template variable: {token.group(0)}")

    parsed = urlsplit(rendered)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OpenMetricsDiscoveryError("resolved endpoint is not an absolute http(s) URL")
    return rendered


class OpenMetricsCheckScheduler:
    """Deterministic bounded scheduler for dynamically discovered targets.

    Scrapes run synchronously in the caller's worker context. A slow endpoint
    therefore cannot create overlapping work or a catch-up storm: after each
    attempt its next deadline is ``now + interval``. The GUI/runtime layer can
    execute :meth:`run_due` in a background thread.
    """

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

    def run_due(self, *, now: float) -> tuple[OpenMetricsScrapeBatch, ...]:
        batches: list[OpenMetricsScrapeBatch] = []
        for key in sorted(self._targets):
            target = self._targets[key]
            health = self._health[key]
            if now < health.next_due:
                continue
            next_due = float(now) + target.interval_s
            try:
                result = self.collector.scrape(
                    target.url,
                    metric_patterns=target.metric_patterns,
                    namespace=target.namespace,
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
