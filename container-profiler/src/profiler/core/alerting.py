"""Deterministic threshold alert state machine for profiler metrics."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional


class AlertStatus(str, Enum):
    OK = "ok"
    PENDING = "pending"
    FIRING = "firing"


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    name: str
    metric: str
    operator: str
    threshold: float
    trigger_for_s: float = 0.0
    recovery_threshold: Optional[float] = None
    severity: str = "warning"

    def __post_init__(self) -> None:
        if self.operator not in {"gt", "gte", "lt", "lte"}:
            raise ValueError(f"unsupported operator: {self.operator}")
        if self.trigger_for_s < 0:
            raise ValueError("trigger_for_s must be >= 0")


@dataclass(frozen=True, slots=True)
class AlertEvent:
    timestamp: float
    rule_name: str
    metric: str
    severity: str
    previous: AlertStatus
    current: AlertStatus
    value: Optional[float]
    message: str


@dataclass(slots=True)
class _RuleState:
    status: AlertStatus = AlertStatus.OK
    pending_since: Optional[float] = None


class AlertEngine:
    """Evaluate threshold rules and emit only state-transition events."""

    def __init__(self, rules: list[ThresholdRule] | tuple[ThresholdRule, ...]) -> None:
        names = [rule.name for rule in rules]
        if len(names) != len(set(names)):
            raise ValueError("rule names must be unique")
        self.rules = tuple(rules)
        self._states = {rule.name: _RuleState() for rule in self.rules}

    @staticmethod
    def _compare(operator: str, value: float, threshold: float) -> bool:
        if operator == "gt":
            return value > threshold
        if operator == "gte":
            return value >= threshold
        if operator == "lt":
            return value < threshold
        return value <= threshold

    @staticmethod
    def _recovered(rule: ThresholdRule, value: float) -> bool:
        threshold = rule.recovery_threshold
        if threshold is None:
            threshold = rule.threshold
        if rule.operator in {"gt", "gte"}:
            return value <= threshold
        return value >= threshold

    def states(self) -> dict[str, AlertStatus]:
        return {name: state.status for name, state in self._states.items()}

    def evaluate(self, timestamp: float, metrics: Mapping[str, object]) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for rule in self.rules:
            raw_value = metrics.get(rule.metric)
            value = None if raw_value is None else float(raw_value)
            state = self._states[rule.name]
            previous = state.status

            if value is None:
                if state.status == AlertStatus.PENDING:
                    state.status = AlertStatus.OK
                    state.pending_since = None
                continue

            breached = self._compare(rule.operator, value, rule.threshold)
            if state.status == AlertStatus.FIRING:
                if self._recovered(rule, value):
                    state.status = AlertStatus.OK
                    state.pending_since = None
            elif breached:
                if rule.trigger_for_s <= 0:
                    state.status = AlertStatus.FIRING
                    state.pending_since = None
                elif state.status == AlertStatus.OK:
                    state.status = AlertStatus.PENDING
                    state.pending_since = timestamp
                elif state.pending_since is not None and timestamp - state.pending_since >= rule.trigger_for_s:
                    state.status = AlertStatus.FIRING
                    state.pending_since = None
            else:
                state.status = AlertStatus.OK
                state.pending_since = None

            if state.status != previous and state.status in {AlertStatus.FIRING, AlertStatus.OK}:
                action = "firing" if state.status == AlertStatus.FIRING else "recovered"
                events.append(
                    AlertEvent(
                        timestamp=timestamp,
                        rule_name=rule.name,
                        metric=rule.metric,
                        severity=rule.severity,
                        previous=previous,
                        current=state.status,
                        value=value,
                        message=f"{rule.name}: {action} ({rule.metric}={value:g})",
                    )
                )
        return events
