"""Durable, bounded outbound delivery queue.

This module is transport-agnostic: producers enqueue immutable payloads, while a
future HTTP forwarder claims due items, acknowledges successful delivery, or
reschedules failures with bounded exponential backoff. SQLite durability keeps
telemetry available across process restarts without allowing an unreachable
upstream to grow disk usage without bound.
"""
from __future__ import annotations

from dataclasses import dataclass
import random
import sqlite3
import time
from pathlib import Path
from typing import Callable


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


class DurableDeliveryQueue:
    """SQLite-backed at-least-once queue with explicit disk backpressure."""

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
        self._dropped = 0
        self._delivered = 0
        self._failed = 0
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0)
        self._db.row_factory = sqlite3.Row
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
            """
        )
        self._db.commit()

    def enqueue(self, kind: str, payload: bytes, *, created_at: float | None = None) -> bool:
        if not kind or len(kind) > 64:
            raise ValueError("kind must contain 1..64 characters")
        data = bytes(payload)
        size = len(data)
        if size > self.max_payload_bytes or size > self.max_bytes:
            self._dropped += 1
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
        while count > self.max_items or total > self.max_bytes:
            oldest = self._db.execute(
                "SELECT id,payload_bytes FROM delivery_queue ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                break
            self._db.execute("DELETE FROM delivery_queue WHERE id=?", (oldest["id"],))
            count -= 1
            total -= int(oldest["payload_bytes"])
            self._dropped += 1

    def due(self, *, now: float | None = None, limit: int = 100) -> tuple[DeliveryItem, ...]:
        if limit <= 0:
            return ()
        when = self._clock() if now is None else float(now)
        rows = self._db.execute(
            "SELECT id,created_at,available_at,attempts,kind,payload FROM delivery_queue "
            "WHERE available_at<=? ORDER BY available_at ASC,id ASC LIMIT ?",
            (when, min(int(limit), 1000)),
        ).fetchall()
        return tuple(DeliveryItem(**dict(row)) for row in rows)

    def acknowledge(self, item_id: int) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM delivery_queue WHERE id=?", (int(item_id),))
        if cursor.rowcount == 1:
            self._delivered += 1
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
    ) -> float | None:
        """Reschedule a failed item and return its next availability timestamp."""
        if base_delay_s <= 0 or max_delay_s <= 0 or not 0 <= jitter <= 1:
            raise ValueError("invalid retry policy")
        row = self._db.execute(
            "SELECT attempts FROM delivery_queue WHERE id=?", (int(item_id),)
        ).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"]) + 1
        delay = min(float(max_delay_s), float(base_delay_s) * (2 ** min(attempts - 1, 30)))
        rv = random.random() if random_value is None else float(random_value)
        rv = max(0.0, min(1.0, rv))
        delay *= 1.0 + jitter * (2.0 * rv - 1.0)
        when = (self._clock() if now is None else float(now)) + max(0.0, delay)
        with self._db:
            self._db.execute(
                "UPDATE delivery_queue SET attempts=?,available_at=? WHERE id=?",
                (attempts, when, int(item_id)),
            )
        self._failed += 1
        return when

    def stats(self, *, now: float | None = None) -> DeliveryQueueStats:
        row = self._db.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(payload_bytes),0) AS b,MIN(created_at) AS oldest "
            "FROM delivery_queue"
        ).fetchone()
        oldest = row["oldest"]
        age = None if oldest is None else max(0.0, (self._clock() if now is None else float(now)) - float(oldest))
        return DeliveryQueueStats(
            queued_items=int(row["n"]), queued_bytes=int(row["b"]), oldest_age_s=age,
            dropped_items=self._dropped, delivered_items=self._delivered,
            failed_attempts=self._failed,
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "DurableDeliveryQueue":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
