"""Hot-reloadable alert runtime with bounded transition history."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .alert_config import AlertConfigError, load_alert_rules, sample_metrics
from .alerting import AlertEngine, AlertEvent, AlertStatus


@dataclass(frozen=True, slots=True)
class AlertRuntimeSnapshot:
    generation: int
    rule_count: int
    states: dict[str, AlertStatus]
    event_count: int
    last_config_error: str | None


class AlertRuntime:
    """Own alert configuration, hot reloads, engine state, and recent events.

    A malformed replacement config never destroys the last known-good engine.
    This mirrors long-running agent behavior: configuration can degrade without
    taking telemetry or already-active alert rules down with it.
    """

    def __init__(self, config_path: str | Path, *, max_events: int = 500) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be > 0")
        self.config_path = Path(config_path)
        self.max_events = max_events
        self.engine = AlertEngine(())
        self.events: deque[AlertEvent] = deque(maxlen=max_events)
        self.generation = 0
        self.last_config_error: str | None = None
        self._signature: object = object()
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
        """Atomically replace the engine when a new config validates.

        Returns ``True`` only when a valid config generation was installed.
        Invalid configs update ``last_config_error`` while preserving the
        previous rules and states.
        """
        signature = self._file_signature()
        if not force and signature == self._signature:
            return False
        self._signature = signature

        try:
            rules = load_alert_rules(self.config_path)
        except AlertConfigError as exc:
            self.last_config_error = str(exc)
            return False

        self.engine = AlertEngine(rules)
        self.generation += 1
        self.last_config_error = None
        return True

    def reload_if_changed(self) -> bool:
        return self.reload(force=False)

    def evaluate(
        self, timestamp: float, stats: Any, power_stats: Any
    ) -> list[AlertEvent]:
        events = self.engine.evaluate(timestamp, sample_metrics(stats, power_stats))
        self.events.extend(events)
        return events

    def snapshot(self) -> AlertRuntimeSnapshot:
        return AlertRuntimeSnapshot(
            generation=self.generation,
            rule_count=len(self.engine.rules),
            states=self.engine.states(),
            event_count=len(self.events),
            last_config_error=self.last_config_error,
        )
