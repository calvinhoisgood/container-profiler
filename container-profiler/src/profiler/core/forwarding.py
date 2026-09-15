"""Durable, bounded outbound delivery and retrying HTTP forwarder.

The queue is transport-agnostic and persists both payloads and cumulative
self-telemetry. A crash after upstream success but before local acknowledgement
may cause a duplicate, which is the expected at-least-once tradeoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class DeliveryItem:
    id: int
    created_at: float
    available_at: float
    attempts: int
    kind: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class DeliveryQueueStats:
    queued_items: int
    queued_bytes: int
    oldest_age_s: float | None
    dropped_items: int
    delivered_items: int
    failed_attempts: int


@dataclass(frozen=True, slots=True)
class DeliveryResponse:
    status: int
    retry_after_s: float | None = None


@dataclass(frozen=True, slots=True)
class ForwarderSnapshot:
    running: bool
    successful_deliveries: int
    retryable_failures: int
    permanent_failures: int
    last_status: int | None
    last_error: str | None
    queue: DeliveryQueueStats


class DeliveryTransport(Protocol):
    def send(self, item: DeliveryItem) -> DeliveryResponse: ...


class DurableDeliveryQueue:
    """SQLite-backed at-least-once queue with explicit disk backpressure."""

    _COUNTERS = ("dropped_items", "delivered_items", "failed_attempts")

    def __init__(
        self,
        path: str | Path,
        *,
        max_items: int = 50_000,
        max_bytes: int = 256 * 1024 * 1024,
        max_payload_bytes: int = 4 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_items <= 0 or max_bytes <= 0 or max_payload_bytes <= 0:
            raise ValueError("queue limits must be positive")
        self.path = str(path)
        self.max_items = int(max_items)
        self.max_bytes = int(max_bytes)
        self.max_payload_bytes = int(max_payload_bytes)
        self._clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS delivery_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                available_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                payload BLOB NOT NULL,
                payload_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_due
                ON delivery_queue(available_at, id);
            CREATE TABLE IF NOT EXISTS delivery_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        for key in self._COUNTERS:
            self._db.execute(
                "INSERT OR IGNORE INTO delivery_meta(key,value) VALUES(?,0)", (key,)
            )
        self._db.commit()

    def _increment(self, key: str, amount: int = 1) -> None:
        self._db.execute(
            "UPDATE delivery_meta SET value=value+? WHERE key=?",
            (int(amount), key),
        )

    def enqueue(self, kind: str, payload: bytes, *, created_at: float | None = None) -> bool:
        if not kind or len(kind) > 64:
            raise ValueError("kind must contain 1..64 characters")
        data = bytes(payload)
        size = len(data)
        with self._lock:
            if size > self.max_payload_bytes or size > self.max_bytes:
                with self._db:
                    self._increment("dropped_items")
                return False
            now = self._clock() if created_at is None else float(created_at)
            with self._db:
                self._db.execute(
                    "INSERT INTO delivery_queue(created_at,available_at,kind,payload,payload_bytes) "
                    "VALUES(?,?,?,?,?)", (now, now, kind, data, size)
                )
                self._enforce_bounds()
        return True

    def _enforce_bounds(self) -> None:
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(payload_bytes),0) AS b FROM delivery_queue"
        ).fetchone()
        count, total = int(row["n"]), int(row["b"])
        dropped = 0
        while count > self.max_items or total > self.max_bytes:
            oldest = self._db.execute(
                "SELECT id,payload_bytes FROM delivery_queue ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                break
            self._db.execute("DELETE FROM delivery_queue WHERE id=?", (oldest["id"],))
            count -= 1
            total -= int(oldest["payload_bytes"])
            dropped += 1
        if dropped:
            self._increment("dropped_items", dropped)

    def due(self, *, now: float | None = None, limit: int = 100) -> tuple[DeliveryItem, ...]:
        if limit <= 0:
            return ()
        when = self._clock() if now is None else float(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT id,created_at,available_at,attempts,kind,payload FROM delivery_queue "
                "WHERE available_at<=? ORDER BY available_at ASC,id ASC LIMIT ?",
                (when, min(int(limit), 1000)),
            ).fetchall()
        return tuple(DeliveryItem(**dict(row)) for row in rows)

    def acknowledge(self, item_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM delivery_queue WHERE id=?", (int(item_id),))
            if cursor.rowcount == 1:
                self._increment("delivered_items")
                return True
        return False

    def discard(self, item_id: int) -> bool:
        """Delete a permanently undeliverable item and account it as dropped."""
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM delivery_queue WHERE id=?", (int(item_id),))
            if cursor.rowcount == 1:
                self._increment("dropped_items")
                return True
        return False

    def retry(
        self,
        item_id: int,
        *,
        now: float | None = None,
        base_delay_s: float = 1.0,
        max_delay_s: float = 300.0,
        jitter: float = 0.2,
        random_value: float | None = None,
        retry_after_s: float | None = None,
    ) -> float | None:
        """Reschedule a failed item and return its next availability timestamp."""
        if base_delay_s <= 0 or max_delay_s <= 0 or not 0 <= jitter <= 1:
            raise ValueError("invalid retry policy")
        if retry_after_s is not None and retry_after_s < 0:
            raise ValueError("retry_after_s must be >= 0")
        with self._lock:
            row = self._db.execute(
                "SELECT attempts FROM delivery_queue WHERE id=?", (int(item_id),)
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            if retry_after_s is None:
                delay = min(
                    float(max_delay_s),
                    float(base_delay_s) * (2 ** min(attempts - 1, 30)),
                )
                rv = random.random() if random_value is None else float(random_value)
                rv = max(0.0, min(1.0, rv))
                delay *= 1.0 + jitter * (2.0 * rv - 1.0)
            else:
                delay = min(float(max_delay_s), float(retry_after_s))
            when = (self._clock() if now is None else float(now)) + max(0.0, delay)
            with self._db:
                self._db.execute(
                    "UPDATE delivery_queue SET attempts=?,available_at=? WHERE id=?",
                    (attempts, when, int(item_id)),
                )
                self._increment("failed_attempts")
            return when

    def stats(self, *, now: float | None = None) -> DeliveryQueueStats:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n,COALESCE(SUM(payload_bytes),0) AS b,MIN(created_at) AS oldest "
                "FROM delivery_queue"
            ).fetchone()
            counters = {
                str(item["key"]): int(item["value"])
                for item in self._db.execute("SELECT key,value FROM delivery_meta").fetchall()
            }
        oldest = row["oldest"]
        age = None if oldest is None else max(
            0.0, (self._clock() if now is None else float(now)) - float(oldest)
        )
        return DeliveryQueueStats(
            queued_items=int(row["n"]),
            queued_bytes=int(row["b"]),
            oldest_age_s=age,
            dropped_items=counters.get("dropped_items", 0),
            delivered_items=counters.get("delivered_items", 0),
            failed_attempts=counters.get("failed_attempts", 0),
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "DurableDeliveryQueue":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def is_retryable_http_status(status: int) -> bool:
    """Classify transient HTTP failures conservatively."""
    code = int(status)
    return code in {408, 425, 429} or 500 <= code <= 599


def _parse_retry_after(value: str | None, *, now: float) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, when - now)


class HTTPDeliveryTransport:
    """Small stdlib HTTP POST transport with per-kind endpoint routing."""

    def __init__(
        self,
        endpoints: Mapping[str, str],
        *,
        timeout_s: float = 5.0,
        headers: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.endpoints = {str(k): str(v) for k, v in endpoints.items()}
        self.timeout_s = float(timeout_s)
        self.headers = dict(headers or {})
        self.clock = clock

    def send(self, item: DeliveryItem) -> DeliveryResponse:
        url = self.endpoints.get(item.kind)
        if not url:
            raise ValueError(f"no forwarding endpoint configured for kind {item.kind!r}")
        headers = {"Content-Type": "application/json", "User-Agent": "container-profiler/5"}
        headers.update(self.headers)
        request = Request(url, data=item.payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status = int(response.status)
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), now=float(self.clock())
                )
                # Consume only a tiny response body so a server cannot force
                # unbounded memory use. Intake APIs do not require response data.
                response.read(4096)
                return DeliveryResponse(status=status, retry_after_s=retry_after)
        except HTTPError as exc:
            return DeliveryResponse(
                status=int(exc.code),
                retry_after_s=_parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None,
                    now=float(self.clock()),
                ),
            )


class DeliveryWorker:
    """Background forwarder with retry classification and bounded batches."""

    def __init__(
        self,
        queue: DurableDeliveryQueue,
        transport: DeliveryTransport,
        *,
        batch_size: int = 100,
        poll_interval_s: float = 1.0,
        base_delay_s: float = 1.0,
        max_delay_s: float = 300.0,
        jitter: float = 0.2,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if batch_size <= 0 or poll_interval_s <= 0:
            raise ValueError("worker limits must be positive")
        self.queue = queue
        self.transport = transport
        self.batch_size = min(int(batch_size), 1000)
        self.poll_interval_s = float(poll_interval_s)
        self.base_delay_s = float(base_delay_s)
        self.max_delay_s = float(max_delay_s)
        self.jitter = float(jitter)
        self.clock = clock
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._successful = 0
        self._retryable = 0
        self._permanent = 0
        self._last_status: int | None = None
        self._last_error: str | None = None

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.is_running():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run,
                name="delivery-forwarder",
                daemon=True,
            )
            self._thread.start()

    def process_once(self, *, now: float | None = None) -> int:
        when = float(self.clock()) if now is None else float(now)
        items = self.queue.due(now=when, limit=self.batch_size)
        processed = 0
        for item in items:
            with self._condition:
                if self._stop_requested:
                    break
            processed += 1
            try:
                response = self.transport.send(item)
            except Exception as exc:
                self.queue.retry(
                    item.id,
                    now=when,
                    base_delay_s=self.base_delay_s,
                    max_delay_s=self.max_delay_s,
                    jitter=self.jitter,
                )
                self._retryable += 1
                self._last_status = None
                self._last_error = str(exc)
                continue

            status = int(response.status)
            self._last_status = status
            if 200 <= status <= 299:
                self.queue.acknowledge(item.id)
                self._successful += 1
                self._last_error = None
            elif is_retryable_http_status(status):
                self.queue.retry(
                    item.id,
                    now=when,
                    base_delay_s=self.base_delay_s,
                    max_delay_s=self.max_delay_s,
                    jitter=self.jitter,
                    retry_after_s=response.retry_after_s,
                )
                self._retryable += 1
                self._last_error = f"HTTP {status}"
            else:
                self.queue.discard(item.id)
                self._permanent += 1
                self._last_error = f"permanent HTTP {status}"
        return processed

    def snapshot(self) -> ForwarderSnapshot:
        return ForwarderSnapshot(
            running=self.is_running(),
            successful_deliveries=self._successful,
            retryable_failures=self._retryable,
            permanent_failures=self._permanent,
            last_status=self._last_status,
            last_error=self._last_error,
            queue=self.queue.stats(),
        )

    def stop(self, timeout_s: float = 6.0) -> bool:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout_s))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stop_requested:
                    break
            processed = self.process_once()
            with self._condition:
                if self._stop_requested:
                    break
                self._condition.wait(0.01 if processed >= self.batch_size else self.poll_interval_s)
