"""Bounded OpenMetrics/Prometheus text ingestion for custom metrics.

The parser deliberately keeps ingestion deterministic and resource-bounded. It
supports Prometheus text and OpenMetrics 1.x sample lines, TYPE/UNIT metadata,
labels, timestamps, metric allow-list regexes, namespaces, and the OpenMetrics
HTTP Accept negotiation used by modern agents. Exemplars are ignored for now.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from typing import Iterable, Pattern
from urllib.request import Request, urlopen


class OpenMetricsError(ValueError):
    """Raised for malformed exposition or unsafe collection configuration."""


@dataclass(frozen=True, slots=True)
class OpenMetricSample:
    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()
    metric_type: str | None = None
    unit: str | None = None
    timestamp_ms: int | None = None

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(f"{key}:{value}" for key, value in self.labels)


@dataclass(frozen=True, slots=True)
class OpenMetricsScrapeResult:
    samples: tuple[OpenMetricSample, ...]
    content_type: str | None
    payload_bytes: int


_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>\S+)(?:\s+(?P<timestamp>-?\d+))?\s*$"
)
_DIRECTIVE_RE = re.compile(r"^#\s*(TYPE|UNIT)\s+(\S+)\s+(\S+)\s*$")


def _unescape_label(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise OpenMetricsError("unterminated label escape")
        escaped = value[index]
        mapping = {"n": "\n", "\\": "\\", '"': '"'}
        if escaped not in mapping:
            raise OpenMetricsError(f"unsupported label escape: \\{escaped}")
        result.append(mapping[escaped])
        index += 1
    return "".join(result)


def _parse_labels(text: str | None, *, max_labels: int) -> tuple[tuple[str, str], ...]:
    if not text:
        return ()

    labels: list[tuple[str, str]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        start = index
        while index < len(text) and (text[index].isalnum() or text[index] == "_"):
            index += 1
        name = text[start:index]
        if not _LABEL_NAME.match(name):
            raise OpenMetricsError("invalid label name")

        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != "=":
            raise OpenMetricsError("label must contain =")
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != '"':
            raise OpenMetricsError("label value must be quoted")
        index += 1

        raw_value: list[str] = []
        while index < len(text):
            if text[index] == '"':
                index += 1
                break
            if text[index] == "\\":
                if index + 1 >= len(text):
                    raise OpenMetricsError("unterminated label escape")
                raw_value.extend((text[index], text[index + 1]))
                index += 2
            else:
                raw_value.append(text[index])
                index += 1
        else:
            raise OpenMetricsError("unterminated label value")

        labels.append((name, _unescape_label("".join(raw_value))))
        if len(labels) > max_labels:
            raise OpenMetricsError("too many labels on one sample")

        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            break
        if text[index] != ",":
            raise OpenMetricsError("expected comma between labels")
        index += 1

    # Canonical ordering prevents duplicate series identities caused only by
    # label ordering differences in exposition output.
    return tuple(sorted(labels))


def _parse_number(raw: str) -> float:
    aliases = {
        "+Inf": float("inf"),
        "Inf": float("inf"),
        "-Inf": float("-inf"),
        "NaN": float("nan"),
    }
    try:
        value = aliases[raw] if raw in aliases else float(raw)
    except ValueError as exc:
        raise OpenMetricsError("sample value is not numeric") from exc
    if not isfinite(value):
        # Persisting NaN/Inf produces surprising SQLite/JSON semantics later in
        # the pipeline. Treat them as unavailable instead of corrupting series.
        raise OpenMetricsError("non-finite metric values are not ingested")
    return value


def parse_openmetrics_text(
    payload: str,
    *,
    metric_patterns: Iterable[str] = (".*",),
    namespace: str = "",
    max_samples: int = 2000,
    max_labels_per_sample: int = 64,
    require_type: bool = False,
) -> tuple[OpenMetricSample, ...]:
    """Parse a bounded Prometheus/OpenMetrics text exposition payload.

    ``metric_patterns`` use full-match regular expressions against raw metric
    names. This is intentionally allow-list oriented: callers can avoid an
    accidental cardinality explosion instead of blindly scraping every series.
    """
    if max_samples <= 0 or max_labels_per_sample <= 0:
        raise ValueError("limits must be positive")

    try:
        patterns: tuple[Pattern[str], ...] = tuple(re.compile(p) for p in metric_patterns)
    except re.error as exc:
        raise OpenMetricsError(f"invalid metric regex: {exc}") from exc
    if not patterns:
        raise OpenMetricsError("at least one metric pattern is required")

    clean_namespace = namespace.rstrip(".")
    if clean_namespace and not _METRIC_NAME.match(clean_namespace):
        raise OpenMetricsError("invalid namespace")

    types: dict[str, str] = {}
    units: dict[str, str] = {}
    samples: list[OpenMetricSample] = []

    for line_number, raw_line in enumerate(payload.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("# HELP") or line.startswith("# EOF"):
            continue
        if line.startswith("#"):
            directive = _DIRECTIVE_RE.match(line)
            if directive:
                kind, name, value = directive.groups()
                (types if kind == "TYPE" else units)[name] = value
            continue

        # OpenMetrics exemplars are metadata for a sample and begin after ` # {`.
        # Keep the metric itself, while trace exemplar routing is a future layer.
        if " # {" in line:
            line = line.split(" # {", 1)[0].rstrip()

        match = _SAMPLE_RE.match(line)
        if not match:
            raise OpenMetricsError(f"line {line_number}: malformed sample")
        name = match.group("name")
        if not any(pattern.fullmatch(name) for pattern in patterns):
            continue

        metric_type = types.get(name)
        if require_type and metric_type is None:
            continue

        labels = _parse_labels(match.group("labels"), max_labels=max_labels_per_sample)
        timestamp = match.group("timestamp")
        samples.append(
            OpenMetricSample(
                name=f"{clean_namespace}.{name}" if clean_namespace else name,
                value=_parse_number(match.group("value")),
                labels=labels,
                metric_type=metric_type,
                unit=units.get(name),
                timestamp_ms=int(timestamp) if timestamp is not None else None,
            )
        )
        if len(samples) > max_samples:
            raise OpenMetricsError(f"metric limit exceeded ({max_samples})")

    return tuple(samples)


class OpenMetricsCollector:
    """HTTP collector with strict payload/sample limits and explicit filtering."""

    ACCEPT = (
        "application/openmetrics-text;version=1.0.0,"
        "application/openmetrics-text;version=0.0.1;q=0.75,"
        "text/plain;version=0.0.4;q=0.5,*/*;q=0.1"
    )

    def __init__(
        self,
        *,
        max_payload_bytes: int = 4 * 1024 * 1024,
        timeout_s: float = 5.0,
    ) -> None:
        if max_payload_bytes <= 0 or timeout_s <= 0:
            raise ValueError("collector limits must be positive")
        self.max_payload_bytes = max_payload_bytes
        self.timeout_s = timeout_s

    def scrape(self, url: str, **parse_kwargs) -> OpenMetricsScrapeResult:
        request = Request(
            url,
            headers={
                "Accept": self.ACCEPT,
                "User-Agent": "container-profiler/5",
            },
        )
        with urlopen(request, timeout=self.timeout_s) as response:
            content_type = response.headers.get("Content-Type")
            raw = response.read(self.max_payload_bytes + 1)

        if len(raw) > self.max_payload_bytes:
            raise OpenMetricsError(f"payload exceeds {self.max_payload_bytes} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OpenMetricsError("metrics endpoint is not valid UTF-8") from exc

        samples = parse_openmetrics_text(text, **parse_kwargs)
        return OpenMetricsScrapeResult(
            samples=samples,
            content_type=content_type,
            payload_bytes=len(raw),
        )
