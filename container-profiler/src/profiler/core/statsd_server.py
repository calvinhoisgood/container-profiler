"""Local UDP transport for the StatsD aggregator.

The listener binds to loopback by default. This mirrors the common local-agent
pattern while avoiding an accidental network-wide unauthenticated UDP service.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from .statsd import AggregatedMetric, StatsDAggregator

FlushCallback = Callable[[list[AggregatedMetric]], None]


class StatsDUDPServer:
    """Background UDP receiver with periodic aggregation flushes."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8125,
        flush_interval_s: float = 10.0,
        aggregator: StatsDAggregator | None = None,
        on_flush: FlushCallback | None = None,
        max_datagram_bytes: int = 65535,
    ) -> None:
        if flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be > 0")
        if not (1 <= max_datagram_bytes <= 65535):
            raise ValueError("max_datagram_bytes must be in [1, 65535]")
        self.host = host
        self.port = port
        self.flush_interval_s = flush_interval_s
        self.aggregator = aggregator or StatsDAggregator()
        self.on_flush = on_flush
        self.max_datagram_bytes = max_datagram_bytes
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.decode_errors = 0
        self.datagrams_received = 0
        self.lines_received = 0
        self.flush_count = 0
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def address(self) -> tuple[str, int] | None:
        if self._socket is None:
            return None
        host, port = self._socket.getsockname()[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        if self.is_running:
            address = self.address
            assert address is not None
            return address

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
            sock.settimeout(min(0.2, max(0.01, self.flush_interval_s / 2)))
        except Exception:
            sock.close()
            raise

        self._socket = sock
        self._stop_event.clear()
        self.last_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="container-profiler-statsd",
            daemon=True,
        )
        self._thread.start()
        address = self.address
        assert address is not None
        return address

    def _emit_flush(self) -> None:
        metrics = self.aggregator.flush()
        if not metrics:
            return
        self.flush_count += 1
        if self.on_flush is not None:
            try:
                self.on_flush(metrics)
            except Exception as exc:
                self.last_error = f"flush callback failed: {exc}"

    def _handle_datagram(self, payload: bytes) -> None:
        self.datagrams_received += 1
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self.decode_errors += 1
            return

        for line in text.splitlines():
            if not line.strip():
                continue
            self.lines_received += 1
            self.aggregator.ingest(line)

    def _run(self) -> None:
        assert self._socket is not None
        next_flush = time.monotonic() + self.flush_interval_s
        try:
            while not self._stop_event.is_set():
                try:
                    payload, _ = self._socket.recvfrom(self.max_datagram_bytes)
                    self._handle_datagram(payload)
                except socket.timeout:
                    pass
                except OSError as exc:
                    if not self._stop_event.is_set():
                        self.last_error = str(exc)
                    break

                now = time.monotonic()
                if now >= next_flush:
                    self._emit_flush()
                    next_flush = now + self.flush_interval_s
        finally:
            self._emit_flush()

    def stop(self, timeout_s: float = 2.0) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        stopped = not thread.is_alive()
        if stopped:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._thread = None
        return stopped

    def __enter__(self) -> "StatsDUDPServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
