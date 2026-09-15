"""Hot-reloadable threshold alerts for persisted custom metric series.

This runtime evaluates exact metric-name rules independently for each bounded
series identity (target/container/tags). It reuses the deterministic alert state
machine while avoiding cross-container or cross-tag state contamination.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .alerting import AlertEngine, AlertEvent, ThresholdRule
from .custom_metrics import CustomMetricPoint

SCHEMA_VERSION = 1
MAX_RULES = 100
ALLOWED_SEVERITIES = frozenset({"info", "warning", "critical"})
_RULE_KEYS = frozenset({
    "name", "metric", "operator", "threshold", "trigger_for_s",
    "recovery_threshold", "severity", "enabled",
})


class MetricAlertConfigError(ValueError):
    """Raised when a custom-metric alert configuration is invalid."""


@dataclass(frozen=True, slots=True)
class MetricAlertTransition:
    event: AlertEvent
    target_key: str | None
    container_id: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricAlertSnapshot:
    generation: int
    rule_count: int
    tracked_series: int
    event_count: int
    evicted_series: int
    invalid_points: int
    last_config_error: str | None


def _finite(value: Any, field: str, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricAlertConfigError(f"rule {name!r}: {field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise MetricAlertConfigError(f"rule {name!r}: {field} must be finite")
    return result


def parse_metric_alert_config(payload: Mapping[str, Any]) -> tuple[ThresholdRule, ...]:
    """Parse strict v1 rules that can target any exact custom metric name."""
    if not isinstance(payload, Mapping):
        raise MetricAlertConfigError("metric alert config root must be an object")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise MetricAlertConfigError(
            f"unsupported metric alert schema_version: {version!r}"
        )
    unknown = set(payload) - {"schema_version", "rules"}
    if unknown:
        raise MetricAlertConfigError(
            f"unknown config fields: {', '.join(sorted(unknown))}"
        )
    raw_rules = payload.get("rules", [])
    if not isinstance(raw_rules, list):
        raise MetricAlertConfigError("rules must be a list")
    if len(raw_rules) > MAX_RULES:
        raise MetricAlertConfigError(f"too many metric alert rules (max {MAX_RULES})")

    rules: list[ThresholdRule] = []
    for index, raw in enumerate(raw_rules, 1):
        if not isinstance(raw, Mapping):
            raise MetricAlertConfigError(f"rule #{index}: must be an object")
        extra = set(raw) - _RULE_KEYS
        if extra:
            raise MetricAlertConfigError(
                f"rule #{index}: unknown fields: {', '.join(sorted(extra))}"
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
            raise MetricAlertConfigError(f"rule #{index}: invalid name")
        name = name.strip()
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MetricAlertConfigError(f"rule {name!r}: enabled must be boolean")
        if not enabled:
            continue
        metric = raw.get("metric")
        if (
            not isinstance(metric, str)
            or not metric.strip()
            or len(metric.strip()) > 256
        ):
            raise MetricAlertConfigError(
                f"rule {name!r}: metric must be a non-empty string"
            )
        metric = metric.strip()
        operator = raw.get("operator")
        if operator not in {"gt", "gte", "lt", "lte"}:
            raise MetricAlertConfigError(
                f"rule {name!r}: unsupported operator {operator!r}"
            )
        threshold = _finite(raw.get("threshold"), "threshold", name)
        trigger_for_s = _finite(
            raw.get("trigger_for_s", 0.0), "trigger_for_s", name
        )
        if trigger_for_s < 0:
            raise MetricAlertConfigError(
                f"rule {name!r}: trigger_for_s must be >= 0"
            )
        recovery_raw = raw.get("recovery_threshold")
        recovery = (
            None
            if recovery_raw is None
            else _finite(recovery_raw, "recovery_threshold", name)
        )
        if recovery is not None:
            if operator in {"gt", "gte"} and recovery > threshold:
                raise MetricAlertConfigError(
                    f"rule {name!r}: high-threshold recovery must be <= threshold"
                )
            if operator in {"lt", "lte"} and recovery < threshold:
                raise MetricAlertConfigError(
                    f"rule {name!r}: low-threshold recovery must be >= threshold"
                )
        severity = raw.get("severity", "warning")
        if severity not in ALLOWED_SEVERITIES:
            raise MetricAlertConfigError(
                f"rule {name!r}: unsupported severity {severity!r}"
            )
        rules.append(
            ThresholdRule(
                name=name,
                metric=metric,
                operator=operator,
                threshold=threshold,
                trigger_for_s=trigger_for_s,
                recovery_threshold=recovery,
                severity=severity,
            )
        )

    names = [rule.name for rule in rules]
    if len(names) != len(set(names)):
        raise MetricAlertConfigError("enabled metric alert rule names must be unique")
    return tuple(rules)


def load_metric_alert_rules(path: str | Path) -> tuple[ThresholdRule, ...]:
    """Load UTF-8 alert rules; a missing file intentionally disables alerts."""
    config_path = Path(path)
    if not config_path.exists():
        return ()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetricAlertConfigError(
            f"cannot read metric alert config {config_path}: {exc}"
        ) from exc
    return parse_metric_alert_config(payload)


class MetricAlertRuntime:
    """Evaluate threshold state independently for each bounded metric series."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        max_series: int = 5000,
        max_events: int = 500,
    ) -> None:
        if max_series <= 0 or max_events <= 0:
            raise ValueError("metric alert bounds must be positive")
        self.config_path = Path(config_path)
        self.max_series = int(max_series)
        self.events: deque[MetricAlertTransition] = deque(maxlen=max_events)
        self.generation = 0
        self.last_config_error: str | None = None
        self.evicted_series = 0
        self.invalid_points = 0
        self._signature: object = object()
        self._rules: tuple[ThresholdRule, ...] = ()
        self._rules_by_metric: dict[str, tuple[ThresholdRule, ...]] = {}
        self._engines: OrderedDict[tuple[object, ...], AlertEngine] = OrderedDict()
        self.reload(force=True)

    def _file_signature(self) -> object:
        try:
            stat = self.config_path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ("error", str(exc))

    def reload(self, *, force: bool = False) -> bool:
        """Install a valid generation atomically, preserving last-good on errors."""
        signature = self._file_signature()
        if not force and signature == self._signature:
            return False
        self._signature = signature
        try:
            rules = load_metric_alert_rules(self.config_path)
        except MetricAlertConfigError as exc:
            self.last_config_error = str(exc)
            return False

        by_metric: dict[str, list[ThresholdRule]] = {}
        for rule in rules:
            by_metric.setdefault(rule.metric, []).append(rule)
        self._rules = rules
        self._rules_by_metric = {
            name: tuple(items) for name, items in by_metric.items()
        }
        # A valid configuration generation intentionally starts fresh state so
        # changed thresholds cannot inherit pending/firing state from old rules.
        self._engines.clear()
        self.generation += 1
        self.last_config_error = None
        return True

    def reload_if_changed(self) -> bool:
        return self.reload(force=False)

    @staticmethod
    def _series_identity(point: CustomMetricPoint) -> tuple[object, ...]:
        return (
            point.target_key or "",
            point.container_id or "",
            tuple(sorted(str(tag) for tag in point.tags)),
        )

    def _engine_for(self, rule: ThresholdRule, point: CustomMetricPoint) -> AlertEngine:
        key = (rule.name, *self._series_identity(point))
        engine = self._engines.get(key)
        if engine is not None:
            self._engines.move_to_end(key)
            return engine
        if len(self._engines) >= self.max_series:
            self._engines.popitem(last=False)
            self.evicted_series += 1
        engine = AlertEngine((rule,))
        self._engines[key] = engine
        return engine

    def evaluate_points(
        self, points: Iterable[CustomMetricPoint]
    ) -> tuple[MetricAlertTransition, ...]:
        transitions: list[MetricAlertTransition] = []
        for point in points:
            rules = self._rules_by_metric.get(point.name)
            if not rules:
                continue
            try:
                value = float(point.value)
                timestamp = float(point.timestamp)
            except (TypeError, ValueError):
                self.invalid_points += 1
                continue
            if not math.isfinite(value) or not math.isfinite(timestamp):
                self.invalid_points += 1
                continue

            for rule in rules:
                engine = self._engine_for(rule, point)
                for event in engine.evaluate(timestamp, {rule.metric: value}):
                    scope = point.target_key or point.container_id or "global"
                    scoped_event = replace(
                        event,
                        message=f"{event.message} target={scope}",
                    )
                    transition = MetricAlertTransition(
                        event=scoped_event,
                        target_key=point.target_key,
                        container_id=point.container_id,
                        tags=tuple(point.tags),
                    )
                    transitions.append(transition)
                    self.events.append(transition)
        return tuple(transitions)

    def snapshot(self) -> MetricAlertSnapshot:
        return MetricAlertSnapshot(
            generation=self.generation,
            rule_count=len(self._rules),
            tracked_series=len(self._engines),
            event_count=len(self.events),
            evicted_series=self.evicted_series,
            invalid_points=self.invalid_points,
            last_config_error=self.last_config_error,
        )