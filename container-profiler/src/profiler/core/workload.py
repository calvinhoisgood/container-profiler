"""Bounded process and log inspection for Docker containers.

This module intentionally keeps Docker-specific calls behind an injected client
so parsing, bounding and failure semantics can be tested without a daemon.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any


@dataclass(slots=True, frozen=True)
class ProcessSnapshot:
    """A single, bounded `docker top` result.

    Docker returns platform-dependent column names, so the snapshot preserves
    those columns rather than pretending Linux and Windows process tables share
    one schema.
    """

    container_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(slots=True, frozen=True)
class ContainerLogLine:
    """One decoded log line with an optional Docker RFC3339 timestamp."""

    timestamp: str | None
    message: str


class DockerWorkloadInspector:
    """Read bounded process and log snapshots through a Docker SDK client."""

    _RFC3339_PREFIX = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s(?P<msg>.*)$"
    )

    def __init__(self, client: Any) -> None:
        self.client = client
        self.last_error: str | None = None

    @staticmethod
    def _validate_limit(name: str, value: int, *, maximum: int) -> int:
        value = int(value)
        if value <= 0 or value > maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return value

    def get_processes(
        self,
        container_id: str,
        *,
        ps_args: str | None = None,
        max_rows: int = 2000,
    ) -> ProcessSnapshot | None:
        """Return a bounded process snapshot without assuming OS-specific columns."""
        max_rows = self._validate_limit("max_rows", max_rows, maximum=10_000)
        try:
            container = self.client.containers.get(container_id)
            kwargs = {"ps_args": ps_args} if ps_args else {}
            raw = container.top(**kwargs) or {}
            titles = tuple(str(value) for value in (raw.get("Titles") or ()))
            raw_rows = raw.get("Processes") or ()
            rows: list[tuple[str, ...]] = []
            for raw_row in raw_rows[:max_rows]:
                row = tuple(str(value) for value in raw_row)
                if titles:
                    if len(row) < len(titles):
                        row = row + ("",) * (len(titles) - len(row))
                    elif len(row) > len(titles):
                        row = row[: len(titles)]
                rows.append(row)
            self.last_error = None
            return ProcessSnapshot(container_id, titles, tuple(rows))
        except Exception as exc:
            self.last_error = str(exc)
            return None

    @classmethod
    def _parse_log_lines(
        cls, text: str, *, max_lines: int
    ) -> tuple[ContainerLogLine, ...]:
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        parsed: list[ContainerLogLine] = []
        for line in lines:
            match = cls._RFC3339_PREFIX.match(line)
            if match:
                parsed.append(ContainerLogLine(match.group("ts"), match.group("msg")))
            else:
                parsed.append(ContainerLogLine(None, line))
        return tuple(parsed)

    def get_logs(
        self,
        container_id: str,
        *,
        tail: int = 200,
        since: int | datetime | None = None,
        max_bytes: int = 1024 * 1024,
        max_lines: int = 5000,
    ) -> tuple[ContainerLogLine, ...] | None:
        """Fetch a bounded log snapshot.

        ``None`` means Docker could not provide logs and ``last_error`` contains
        the reason. An empty tuple means the request succeeded but no lines were
        available. Both byte and text SDK payloads are accepted. If a byte cap
        cuts into a line, that partial first line is discarded.
        """
        tail = self._validate_limit("tail", tail, maximum=100_000)
        max_bytes = self._validate_limit(
            "max_bytes", max_bytes, maximum=64 * 1024 * 1024
        )
        max_lines = self._validate_limit("max_lines", max_lines, maximum=100_000)

        try:
            container = self.client.containers.get(container_id)
            payload = container.logs(
                stdout=True,
                stderr=True,
                stream=False,
                timestamps=True,
                tail=tail,
                since=since,
            )
            if isinstance(payload, str):
                raw = payload.encode("utf-8", errors="replace")
            else:
                raw = bytes(payload or b"")

            if len(raw) > max_bytes:
                raw = raw[-max_bytes:]
                newline = raw.find(b"\n")
                if newline >= 0:
                    raw = raw[newline + 1 :]
                else:
                    raw = b""

            text = raw.decode("utf-8", errors="replace")
            result = self._parse_log_lines(text, max_lines=max_lines)
            self.last_error = None
            return result
        except Exception as exc:
            self.last_error = str(exc)
            return None
