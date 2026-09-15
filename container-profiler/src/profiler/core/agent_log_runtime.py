"""Compose durable container-log ingestion with the existing headless Agent runtime."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, is_dataclass
import threading
from typing import Any

from .agent_runtime import AgentRuntimeSnapshot, LocalAgentProcess, LocalAgentRuntime
from .container_logs import ContainerLogWorker
from .log_storage import ContainerLogRepository
from .storage import SQLiteTelemetryStore


class AgentLogRuntimeSnapshot(Mapping[str, Any]):
    """Attribute-compatible Agent snapshot that is also JSON-safe as a Mapping."""

    __slots__ = (
        "base", "logs_persisted", "log_storage_failures",
        "last_log_storage_error", "logs", "_mapping",
    )

    def __init__(
        self,
        base: AgentRuntimeSnapshot,
        *,
        logs_persisted: int,
        log_storage_failures: int,
        last_log_storage_error: str | None,
        logs: Any | None,
    ) -> None:
        self.base = base
        self.logs_persisted = int(logs_persisted)
        self.log_storage_failures = int(log_storage_failures)
        self.last_log_storage_error = last_log_storage_error
        self.logs = logs
        values = asdict(base)
        values.update(
            logs_persisted=self.logs_persisted,
            log_storage_failures=self.log_storage_failures,
            last_log_storage_error=self.last_log_storage_error,
            logs=asdict(logs) if logs is not None and is_dataclass(logs) else logs,
        )
        self._mapping = values

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def __getitem__(self, key: str) -> Any:
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


class LogAwareAgentRuntime(LocalAgentRuntime):
    """Add opt-in Docker logs without coupling the base metric runtime to text data."""

    def __init__(
        self,
        store: SQLiteTelemetryStore,
        *,
        log_worker: ContainerLogWorker | None = None,
        log_retention_s: float = 7 * 24 * 60 * 60,
        log_retention_max_rows: int = 500_000,
        log_retention_interval_s: float = 300.0,
        **runtime_kwargs,
    ) -> None:
        if log_retention_s <= 0 or log_retention_max_rows <= 0 or log_retention_interval_s <= 0:
            raise ValueError("log retention settings must be positive")
        super().__init__(store, **runtime_kwargs)
        self.log_worker = log_worker
        self.log_repository = ContainerLogRepository(store) if log_worker is not None else None
        self.log_retention_s = float(log_retention_s)
        self.log_retention_max_rows = int(log_retention_max_rows)
        self.log_retention_interval_s = float(log_retention_interval_s)
        self._logs_persisted = 0
        self._log_storage_failures = 0
        self._last_log_storage_error: str | None = None
        self._next_log_retention = float("inf")

    def start(self):
        if self._running:
            return
        super().start()
        try:
            if self.log_worker is not None:
                if self.log_repository is not None:
                    try:
                        max_replay = int(
                            getattr(self.log_worker, "dedupe_entries_per_container", 4096)
                        )
                        cursors, recent = self.log_repository.replay_state(
                            max_entries_per_container=max_replay
                        )
                        restore = getattr(self.log_worker, "restore_state", None)
                        if callable(restore):
                            restore(cursors, recent)
                    except Exception as exc:
                        self._log_storage_failures += 1
                        self._last_log_storage_error = f"replay state: {exc}"
                self.log_worker.start()
                self._next_log_retention = self.monotonic_clock() + self.log_retention_interval_s
        except Exception:
            if self.log_worker is not None:
                try:
                    self.log_worker.stop(timeout_s=2)
                except Exception:
                    pass
            super().stop()
            raise

    def _drain_logs(self) -> None:
        worker = self.log_worker
        repository = self.log_repository
        if worker is None or repository is None:
            return
        records = worker.drain_records(self.drain_limit)
        if not records:
            return
        try:
            self._logs_persisted += repository.append(records)
            self._last_log_storage_error = None
        except Exception as exc:
            worker.requeue_records(records)
            self._log_storage_failures += 1
            self._last_log_storage_error = str(exc)

    def _log_retention(self) -> None:
        repository = self.log_repository
        if repository is None:
            return
        now = self.monotonic_clock()
        if now < self._next_log_retention:
            return
        try:
            repository.prune(
                before_timestamp=self.wall_clock() - self.log_retention_s,
                max_rows=self.log_retention_max_rows,
            )
            self._last_retention_error = None
        except Exception as exc:
            self._retention_failures += 1
            self._last_retention_error = str(exc)
        finally:
            self._next_log_retention = now + self.log_retention_interval_s

    def run_once(self):
        super().run_once()
        self._drain_logs()
        self._log_retention()

    def stop(self):
        if not self._running:
            return True
        log_ok = True
        if self.log_worker is not None:
            log_ok = bool(self.log_worker.stop(timeout_s=2))
            self._drain_logs()
        base_ok = bool(super().stop())
        return log_ok and base_ok

    def snapshot(self) -> AgentLogRuntimeSnapshot:
        base = super().snapshot()
        logs = None
        if self.log_worker is not None:
            try:
                logs = self.log_worker.snapshot()
            except Exception:
                logs = None
        return AgentLogRuntimeSnapshot(
            base,
            logs_persisted=self._logs_persisted,
            log_storage_failures=self._log_storage_failures,
            last_log_storage_error=self._last_log_storage_error,
            logs=logs,
        )


class LogAwareAgentProcess(LocalAgentProcess):
    """Windows-service compatible process wrapper using ``LogAwareAgentRuntime``."""

    def run(self, stop_event: threading.Event):
        with SQLiteTelemetryStore(self.database_path) as store:
            LogAwareAgentRuntime(store, **self.runtime_kwargs).run_forever(stop_event)
