"""Validated JSON configuration for threshold alerts."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from .alerting import ThresholdRule

SCHEMA_VERSION = 1
MAX_RULES = 100
ALLOWED_METRICS = frozenset({
    "cpu_percent",
    "memory_mb",
    "memory_percent",
    "network_rx_bps",
    "network_tx_bps",
    "pids",
    "cpu_power_w",
    "gpu_power_w",
    "gpu_util_percent",
    "gpu_memory_mb",
    "gpu_memory_total_mb",
    "gpu_temp_c",
})
ALLOWED_SEVERITIES = frozenset({"info", "warning", "critical"})
_RULE_KEYS = frozenset({
    "name",
    "metric",
    "operator",
    "threshold",
    "trigger_for_s",
    "recovery_threshold",
    "severity",
    "enabled",
})


class AlertConfigError(ValueError):
    """Raised when an alert configuration is unsafe or malformed."""


def _finite_number(value: Any, field: str, rule_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlertConfigError(f"rule {rule_name!r}: {field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise AlertConfigError(f"rule {rule_name!r}: {field} must be finite")
    return number


def _parse_rule(raw: Mapping[str, Any], index: int) -> ThresholdRule | None:
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise AlertConfigError(
            f"rule #{index}: unknown fields: {', '.join(sorted(unknown))}"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AlertConfigError(f"rule #{index}: name must be a non-empty string")
    name = name.strip()

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AlertConfigError(f"rule {name!r}: enabled must be boolean")
    if not enabled:
        return None

    metric = raw.get("metric")
    if metric not in ALLOWED_METRICS:
        raise AlertConfigError(f"rule {name!r}: unsupported metric {metric!r}")

    operator = raw.get("operator")
    if operator not in {"gt", "gte", "lt", "lte"}:
        raise AlertConfigError(f"rule {name!r}: unsupported operator {operator!r}")

    threshold = _finite_number(raw.get("threshold"), "threshold", name)
    trigger_for_s = _finite_number(
        raw.get("trigger_for_s", 0.0), "trigger_for_s", name
    )
    if trigger_for_s < 0:
        raise AlertConfigError(f"rule {name!r}: trigger_for_s must be >= 0")

    recovery = raw.get("recovery_threshold")
    recovery_threshold = (
        None
        if recovery is None
        else _finite_number(recovery, "recovery_threshold", name)
    )
    if recovery_threshold is not None:
        if operator in {"gt", "gte"} and recovery_threshold > threshold:
            raise AlertConfigError(
                f"rule {name!r}: high-threshold recovery must be <= threshold"
            )
        if operator in {"lt", "lte"} and recovery_threshold < threshold:
            raise AlertConfigError(
                f"rule {name!r}: low-threshold recovery must be >= threshold"
            )

    severity = raw.get("severity", "warning")
    if severity not in ALLOWED_SEVERITIES:
        raise AlertConfigError(
            f"rule {name!r}: unsupported severity {severity!r}"
        )

    return ThresholdRule(
        name=name,
        metric=metric,
        operator=operator,
        threshold=threshold,
        trigger_for_s=trigger_for_s,
        recovery_threshold=recovery_threshold,
        severity=severity,
    )


def parse_alert_config(payload: Mapping[str, Any]) -> tuple[ThresholdRule, ...]:
    """Parse and validate an alert configuration object.

    The schema is intentionally strict so a typo cannot silently create a rule
    with different semantics. Disabled rules are validated enough to identify
    them, then omitted from the runtime engine.
    """
    if not isinstance(payload, Mapping):
        raise AlertConfigError("alert config root must be an object")

    version = payload.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise AlertConfigError(f"unsupported alert schema_version: {version!r}")

    unknown = set(payload) - {"schema_version", "rules"}
    if unknown:
        raise AlertConfigError(
            f"unknown config fields: {', '.join(sorted(unknown))}"
        )

    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        raise AlertConfigError("rules must be a list")
    if len(rules) > MAX_RULES:
        raise AlertConfigError(f"too many alert rules (max {MAX_RULES})")

    parsed: list[ThresholdRule] = []
    for index, raw in enumerate(rules, 1):
        if not isinstance(raw, Mapping):
            raise AlertConfigError(f"rule #{index}: must be an object")
        rule = _parse_rule(raw, index)
        if rule is not None:
            parsed.append(rule)

    names = [rule.name for rule in parsed]
    if len(names) != len(set(names)):
        raise AlertConfigError("enabled alert rule names must be unique")
    return tuple(parsed)


def load_alert_rules(path: str | Path) -> tuple[ThresholdRule, ...]:
    """Load rules from UTF-8 JSON; a missing file means alerts are disabled."""
    config_path = Path(path)
    if not config_path.exists():
        return ()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlertConfigError(f"cannot read alert config {config_path}: {exc}") from exc
    return parse_alert_config(payload)


def sample_metrics(stats: Any, power_stats: Any) -> dict[str, float | None]:
    """Flatten one normalized resource/power sample for alert evaluation."""
    values: dict[str, float | None] = {}
    for name in ALLOWED_METRICS:
        value = getattr(stats, name, None)
        if value is None:
            value = getattr(power_stats, name, None)
        values[name] = value
    return values
