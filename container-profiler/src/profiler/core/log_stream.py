"""Bounded, cancellable container log streaming primitives."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Any, Callable, Iterable

from .workload import ContainerLogLine, DockerWorkloadInspector


@dataclass(slots=True, frozen=True)
class LogBufferStats:
    enqueued: int
    drained: int
    dropped: int
    queued: int
    queued_bytes: int
    peak_queued: int


class BoundedLogBuffer:
    """Thread-safe drop-oldest queue bounded by line count and encoded bytes."""

    def __init__(self, *, max_lines: int = 5000, max_bytes: int = 4 * 1024 * 1024) -> None:
        if max_lines <= 0 or max_bytes <= 0:
            raise ValueError("log buffer limits must be > 0")
        self.max_lines = int(max_lines)
        self.max_bytes = int(max_bytes)
        self._items: deque[tuple[ContainerLogLine, int]] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._enqueued = self._drained = self._dropped = self._peak = 0

    @staticmethod
    def _size(line: ContainerLogLine) -> int:
        return len((line.timestamp or "").encode("utf-8", errors="replace")) + len(
            line.message.encode("utf-8", errors="replace")
        ) + 1

    def push(self, line: ContainerLogLine) -> bool:
        size = self._size(line)
        with self._lock:
            if size > self.max_bytes:
                self._dropped += 1
                return False
            while self._items and (
                len(self._items) >= self.max_lines or self._bytes + size > self.max_bytes
            ):
                _, old_size = self._items.popleft()
                self._bytes -= old_size
                self._dropped += 1
            self._items.append((line, size))
            self._bytes += size
            self._enqueued += 1
            self._peak = max(self._peak, len(self._items))
            return True

    def extend(self, lines: Iterable[ContainerLogLine]) -> int:
        return sum(int(self.push(line)) for line in lines)

    def drain(self, max_items: int = 250) -> tuple[ContainerLogLine, ...]:
        if max_items <= 0:
            raise ValueError("max_items must be > 0")
        with self._lock:
            count = min(int(max_items), len(self._items))
            output = []
            for _ in range(count):
                line, size = self._items.popleft()
                self._bytes -= size
                output.append(line)
            self._drained += count
            return tuple(output)

    def stats(self) -> LogBufferStats:
        with self._lock:
            return LogBufferStats(
                self._enqueued, self._drained, self._dropped, len(self._items),
                self._bytes, self._peak,
            )


class LogStreamDecoder:
    """Split arbitrary Docker chunks while bounding unterminated fragments."""

    def __init__(self, *, max_pending_bytes: int = 256 * 1024) -> None:
        if max_pending_bytes <= 0:
            raise ValueError("max_pending_bytes must be > 0")
        self.max_pending_bytes = int(max_pending_bytes)
        self._pending = bytearray()
        self.dropped_fragments = 0

    def feed(self, chunk: bytes | str) -> tuple[ContainerLogLine, ...]:
        raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
        if not raw:
            return ()
        self._pending.extend(raw)
        if len(self._pending) > self.max_pending_bytes and b"\n" not in self._pending:
            self._pending.clear()
            self.dropped_fragments += 1
            return ()
        parts = self._pending.split(b"\n")
        self._pending = bytearray(parts.pop())
        if len(self._pending) > self.max_pending_bytes:
            self._pending.clear()
            self.dropped_fragments += 1
        if not parts:
            return ()
        text = b"\n".join(parts).decode("utf-8", errors="replace")
        return DockerWorkloadInspector._parse_log_lines(text, max_lines=max(1, len(parts)))

    def finish(self) -> tuple[ContainerLogLine, ...]:
        if not self._pending:
            return ()
        raw = bytes(self._pending)
        self._pending.clear()
        return DockerWorkloadInspector._parse_log_lines(
            raw.decode("utf-8", errors="replace"), max_lines=1
        )


class DockerLogFollower:
    """Feed Docker's follow stream into a bounded buffer with cancellation."""

    def __init__(self, client: Any, container_id: str, buffer: BoundedLogBuffer, *, tail: int = 100) -> None:
        if tail <= 0 or tail > 100_000:
            raise ValueError("tail must be between 1 and 100000")
        self.client = client
        self.container_id = container_id
        self.buffer = buffer
        self.tail = int(tail)
        self.decoder = LogStreamDecoder()
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._stream: Any = None
        self._lock = threading.Lock()

    @property
    def dropped_fragments(self) -> int:
        return self.decoder.dropped_fragments

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            stream = self._stream
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def run(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        predicate = should_stop or (lambda: False)
        try:
            container = self.client.containers.get(self.container_id)
            stream = container.logs(
                stdout=True, stderr=True, stream=True, follow=True,
                timestamps=True, tail=self.tail,
            )
            with self._lock:
                self._stream = stream
            for chunk in stream:
                if self._stop.is_set() or predicate():
                    break
                self.buffer.extend(self.decoder.feed(chunk))
            if not self._stop.is_set() and not predicate():
                self.buffer.extend(self.decoder.finish())
            self.last_error = None
        except Exception as exc:
            if not self._stop.is_set() and not predicate():
                self.last_error = str(exc)
        finally:
            with self._lock:
                stream, self._stream = self._stream, None
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
