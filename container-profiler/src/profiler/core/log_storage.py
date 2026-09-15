"""Durable, bounded-query storage for headless Docker container logs.

The repository is an additive extension over ``SQLiteTelemetryStore``. Keeping
log text out of the fixed custom-metric schema avoids high-volume strings from
polluting metric paths while still sharing the Agent's WAL database/transaction
boundary. The table is created idempotently when log collection/query is used.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ContainerLogRecord:
    timestamp: float
    container_id: str
    message: str
    docker_timestamp: str | None = None
    container_name: str | None = None
    tags: tuple[str, ...] = ()


class ContainerLogRepository:
    """Store and query container logs through an existing telemetry connection."""

    def __init__(self, telemetry_store: Any) -> None:
        db = getattr(telemetry_store, "_db", None)
        if db is None:
            raise TypeError("telemetry_store does not expose a SQLite connection")
        self.store = telemetry_store
        self._db = db
        self._migrate()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS container_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                docker_timestamp TEXT,
                container_id TEXT NOT NULL,
                container_name TEXT,
                message TEXT NOT NULL,
                message_bytes INTEGER NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_container_logs_timestamp
                ON container_logs(timestamp DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_container_logs_container_timestamp
                ON container_logs(container_id, timestamp DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_container_logs_name_timestamp
                ON container_logs(container_name, timestamp DESC, id DESC);
            """
        )
        self._db.commit()

    def append(self, records: Iterable[ContainerLogRecord]) -> int:
        rows = []
        for record in records:
            container_id = str(record.container_id).strip()
            if not container_id:
                raise ValueError("container_id must not be empty")
            message = str(record.message)
            rows.append(
                (
                    float(record.timestamp),
                    record.docker_timestamp,
                    container_id,
                    record.container_name,
                    message,
                    len(message.encode("utf-8", errors="replace")),
                    json.dumps(tuple(record.tags), ensure_ascii=False, separators=(",", ":")),
                )
            )
        if not rows:
            return 0
        with self._db:
            self._db.executemany(
                """
                INSERT INTO container_logs(
                    timestamp, docker_timestamp, container_id, container_name,
                    message, message_bytes, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def query(
        self,
        *,
        container_id: str | None = None,
        container_name: str | None = None,
        contains: str | None = None,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        before_timestamp: float | None = None,
        before_id: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return newest-first bounded log rows with literal substring search.

        Pagination uses the same ``(timestamp DESC, id DESC)`` ordering as the
        query. Callers must supply both cursor components so out-of-order Docker
        timestamps cannot create gaps or duplicates between pages.
        """
        if limit <= 0:
            return []
        if (before_timestamp is None) != (before_id is None):
            raise ValueError("before_timestamp and before_id must be provided together")
        clauses: list[str] = []
        params: list[Any] = []
        if container_id is not None:
            clauses.append("container_id=?")
            params.append(str(container_id))
        if container_name is not None:
            clauses.append("container_name=?")
            params.append(str(container_name))
        if contains is not None:
            clauses.append("message LIKE ? ESCAPE '\\'")
            params.append("%" + self._escape_like(str(contains)) + "%")
        if start_timestamp is not None:
            clauses.append("timestamp>=?")
            params.append(float(start_timestamp))
        if end_timestamp is not None:
            clauses.append("timestamp<=?")
            params.append(float(end_timestamp))
        if before_timestamp is not None and before_id is not None:
            clauses.append("(timestamp<? OR (timestamp=? AND id<?))")
            cursor_timestamp = float(before_timestamp)
            params.extend((cursor_timestamp, cursor_timestamp, int(before_id)))
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        params.append(min(int(limit), 5000))
        rows = self._db.execute(
            "SELECT id,timestamp,docker_timestamp,container_id,container_name,"
            f"message,message_bytes,tags_json FROM container_logs {where} "
            "ORDER BY timestamp DESC,id DESC LIMIT ?",
            params,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = tuple(json.loads(item.pop("tags_json")))
            except (TypeError, ValueError):
                item.pop("tags_json", None)
                item["tags"] = ()
            result.append(item)
        return result

    def prune(
        self,
        *,
        before_timestamp: float | None = None,
        max_rows: int | None = None,
    ) -> int:
        if before_timestamp is None and max_rows is None:
            return 0
        if max_rows is not None and int(max_rows) < 0:
            raise ValueError("max_rows must be >= 0")
        deleted = 0
        with self._db:
            if before_timestamp is not None:
                cursor = self._db.execute(
                    "DELETE FROM container_logs WHERE timestamp<?",
                    (float(before_timestamp),),
                )
                deleted += max(0, int(cursor.rowcount))
            if max_rows is not None:
                row = self._db.execute("SELECT COUNT(*) FROM container_logs").fetchone()
                count = int(row[0]) if row is not None else 0
                excess = max(0, count - int(max_rows))
                if excess:
                    cursor = self._db.execute(
                        "DELETE FROM container_logs WHERE id IN ("
                        "SELECT id FROM container_logs ORDER BY timestamp ASC,id ASC LIMIT ?)",
                        (excess,),
                    )
                    deleted += max(0, int(cursor.rowcount))
        return deleted
