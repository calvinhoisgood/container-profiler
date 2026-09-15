"""Durable local telemetry, alert, custom-metric, and host store backed by SQLite.

The store is intentionally independent from Qt and Docker so it can be used by
future agent/service and GUI processes. WAL mode allows a writer and explorers
to coexist without making the sampling path depend on the UI.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .alerting import AlertEvent
from .custom_metrics import CustomMetricPoint
from .host_monitor import HostStats


class SQLiteTelemetryStore:
    SCHEMA_VERSION = 4

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                container_id TEXT,
                started_at TEXT,
                ended_at TEXT,
                target_interval_ms INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                elapsed_s REAL NOT NULL,
                cpu_percent REAL,
                memory_mb REAL,
                memory_limit_mb REAL,
                memory_percent REAL,
                network_rx_bytes INTEGER,
                network_tx_bytes INTEGER,
                network_rx_bps REAL,
                network_tx_bps REAL,
                pids INTEGER,
                cpu_power_w REAL,
                gpu_power_w REAL,
                gpu_util_percent REAL,
                gpu_memory_mb REAL,
                gpu_memory_total_mb REAL,
                gpu_temp_c REAL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_samples_session_elapsed
                ON samples(session_id, elapsed_s);
            CREATE INDEX IF NOT EXISTS idx_samples_timestamp
                ON samples(timestamp);
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                container_id TEXT,
                timestamp REAL NOT NULL,
                rule_name TEXT NOT NULL,
                metric TEXT NOT NULL,
                severity TEXT NOT NULL,
                previous_status TEXT NOT NULL,
                current_status TEXT NOT NULL,
                value REAL,
                message TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                note TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_alert_events_timestamp
                ON alert_events(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_alert_events_filters
                ON alert_events(current_status, severity, rule_name);
            CREATE INDEX IF NOT EXISTS idx_alert_events_session
                ON alert_events(session_id, timestamp DESC);
            CREATE TABLE IF NOT EXISTS custom_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                metric_type TEXT,
                unit TEXT,
                source TEXT NOT NULL,
                target_key TEXT,
                container_id TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_custom_metrics_timestamp
                ON custom_metrics(timestamp DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_custom_metrics_name_timestamp
                ON custom_metrics(name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_custom_metrics_container_timestamp
                ON custom_metrics(container_id, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_custom_metrics_target_timestamp
                ON custom_metrics(target_key, timestamp DESC);
            CREATE TABLE IF NOT EXISTS host_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                cpu_percent REAL,
                logical_cpus INTEGER NOT NULL,
                memory_total_mb REAL NOT NULL,
                memory_available_mb REAL NOT NULL,
                memory_used_mb REAL NOT NULL,
                memory_percent REAL NOT NULL,
                uptime_s REAL NOT NULL,
                load_1 REAL,
                load_5 REAL,
                load_15 REAL
            );
            CREATE INDEX IF NOT EXISTS idx_host_samples_timestamp
                ON host_samples(timestamp DESC, id DESC);
            """
        )
        self._db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self.SCHEMA_VERSION),),
        )
        self._db.commit()

    def start_session(
        self,
        session_id: str,
        *,
        container_id: str | None = None,
        started_at: str | None = None,
        target_interval_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False, separators=(",", ":"))
        with self._db:
            self._db.execute(
                """
                INSERT INTO sessions(
                    session_id, container_id, started_at, target_interval_ms, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, container_id, started_at, target_interval_ms, payload),
            )

    SAMPLE_COLUMNS = (
        "timestamp", "elapsed_s", "cpu_percent", "memory_mb", "memory_limit_mb",
        "memory_percent", "network_rx_bytes", "network_tx_bytes", "network_rx_bps",
        "network_tx_bps", "pids", "cpu_power_w", "gpu_power_w", "gpu_util_percent",
        "gpu_memory_mb", "gpu_memory_total_mb", "gpu_temp_c",
    )

    def append_sample(self, session_id: str, sample: Mapping[str, Any]) -> None:
        values = [sample.get(column) for column in self.SAMPLE_COLUMNS]
        with self._db:
            self._db.execute(
                f"INSERT INTO samples(session_id, {','.join(self.SAMPLE_COLUMNS)}) "
                f"VALUES ({','.join(['?'] * (len(self.SAMPLE_COLUMNS) + 1))})",
                [session_id, *values],
            )

    def append_samples(self, session_id: str, samples: Iterable[Mapping[str, Any]]) -> int:
        rows = [[session_id, *[sample.get(column) for column in self.SAMPLE_COLUMNS]] for sample in samples]
        if not rows:
            return 0
        placeholders = ",".join(["?"] * (len(self.SAMPLE_COLUMNS) + 1))
        sql = f"INSERT INTO samples(session_id, {','.join(self.SAMPLE_COLUMNS)}) VALUES ({placeholders})"
        with self._db:
            self._db.executemany(sql, rows)
        return len(rows)

    def finish_session(self, session_id: str, *, ended_at: str | None = None) -> None:
        with self._db:
            cursor = self._db.execute(
                "UPDATE sessions SET ended_at=? WHERE session_id=?",
                (ended_at, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(session_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT s.*, COUNT(p.id) AS sample_count "
            "FROM sessions s LEFT JOIN samples p ON p.session_id=s.session_id "
            "GROUP BY s.session_id ORDER BY COALESCE(s.started_at, '') DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def query_samples(
        self,
        session_id: str,
        *,
        start_elapsed_s: float | None = None,
        end_elapsed_s: float | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses = ["session_id=?"]
        params: list[Any] = [session_id]
        if start_elapsed_s is not None:
            clauses.append("elapsed_s>=?")
            params.append(start_elapsed_s)
        if end_elapsed_s is not None:
            clauses.append("elapsed_s<=?")
            params.append(end_elapsed_s)
        params.append(limit)
        rows = self._db.execute(
            f"SELECT {','.join(self.SAMPLE_COLUMNS)} FROM samples "
            f"WHERE {' AND '.join(clauses)} ORDER BY elapsed_s ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def append_alert_event(
        self,
        event: AlertEvent,
        *,
        session_id: str | None = None,
        container_id: str | None = None,
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO alert_events(
                    session_id, container_id, timestamp, rule_name, metric, severity,
                    previous_status, current_status, value, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, container_id, float(event.timestamp), event.rule_name,
                    event.metric, event.severity, event.previous.value, event.current.value,
                    event.value, event.message,
                ),
            )
        return int(cursor.lastrowid)

    def query_alert_events(
        self,
        *,
        session_id: str | None = None,
        container_id: str | None = None,
        rule_name: str | None = None,
        severity: str | None = None,
        current_status: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id), ("container_id", container_id),
            ("rule_name", rule_name), ("severity", severity),
            ("current_status", current_status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if acknowledged is True:
            clauses.append("acknowledged_at IS NOT NULL")
        elif acknowledged is False:
            clauses.append("acknowledged_at IS NULL")
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        params.append(limit)
        rows = self._db.execute(
            "SELECT id, session_id, container_id, timestamp, rule_name, metric, severity, "
            "previous_status, current_status, value, message, acknowledged_at, "
            f"acknowledged_by, note FROM alert_events {where} "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert_event(
        self,
        event_id: int,
        *,
        acknowledged_by: str | None = None,
        note: str | None = None,
        acknowledged_at: str | None = None,
    ) -> bool:
        when = acknowledged_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._db:
            cursor = self._db.execute(
                """
                UPDATE alert_events SET acknowledged_at=?, acknowledged_by=?, note=?
                WHERE id=? AND acknowledged_at IS NULL
                """,
                (when, acknowledged_by, note, int(event_id)),
            )
        return cursor.rowcount == 1

    def append_custom_metrics(self, points: Iterable[CustomMetricPoint]) -> int:
        rows = [
            (
                float(point.timestamp), point.name, float(point.value), point.metric_type,
                point.unit, point.source, point.target_key, point.container_id,
                json.dumps(point.tags, ensure_ascii=False, separators=(",", ":")),
            )
            for point in points
        ]
        if not rows:
            return 0
        with self._db:
            self._db.executemany(
                """
                INSERT INTO custom_metrics(
                    timestamp, name, value, metric_type, unit, source,
                    target_key, container_id, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def query_custom_metrics(
        self,
        *,
        name: str | None = None,
        name_prefix: str | None = None,
        container_id: str | None = None,
        target_key: str | None = None,
        source: str | None = None,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        limit = min(limit, 10_000)
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("name", name), ("container_id", container_id),
            ("target_key", target_key), ("source", source),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if name_prefix is not None:
            escaped = name_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
        if start_timestamp is not None:
            clauses.append("timestamp>=?")
            params.append(float(start_timestamp))
        if end_timestamp is not None:
            clauses.append("timestamp<=?")
            params.append(float(end_timestamp))
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        params.append(int(limit))
        rows = self._db.execute(
            "SELECT id, timestamp, name, value, metric_type, unit, source, "
            f"target_key, container_id, tags_json FROM custom_metrics {where} "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = tuple(json.loads(item.pop("tags_json")))
            except (TypeError, ValueError):
                item["tags"] = ()
            result.append(item)
        return result

    def list_custom_metric_names(self, *, prefix: str = "", limit: int = 200) -> list[str]:
        if limit <= 0:
            return []
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._db.execute(
            "SELECT DISTINCT name FROM custom_metrics "
            "WHERE name LIKE ? ESCAPE '\\' ORDER BY name ASC LIMIT ?",
            (escaped + "%", min(int(limit), 1000)),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def prune_custom_metrics(
        self,
        *,
        before_timestamp: float | None = None,
        max_rows: int | None = None,
    ) -> int:
        if max_rows is not None and max_rows < 0:
            raise ValueError("max_rows must be >= 0")
        deleted = 0
        with self._db:
            if before_timestamp is not None:
                cursor = self._db.execute(
                    "DELETE FROM custom_metrics WHERE timestamp<?", (float(before_timestamp),)
                )
                deleted += cursor.rowcount
            if max_rows is not None:
                count = int(self._db.execute("SELECT COUNT(*) FROM custom_metrics").fetchone()[0])
                excess = max(0, count - int(max_rows))
                if excess:
                    cursor = self._db.execute(
                        "DELETE FROM custom_metrics WHERE id IN ("
                        "SELECT id FROM custom_metrics ORDER BY timestamp ASC, id ASC LIMIT ?)",
                        (excess,),
                    )
                    deleted += cursor.rowcount
        return deleted

    def append_host_samples(self, samples: Iterable[HostStats]) -> int:
        rows = [
            (
                float(sample.timestamp), sample.cpu_percent, int(sample.logical_cpus),
                float(sample.memory_total_mb), float(sample.memory_available_mb),
                float(sample.memory_used_mb), float(sample.memory_percent),
                float(sample.uptime_s), sample.load_1, sample.load_5, sample.load_15,
            )
            for sample in samples
        ]
        if not rows:
            return 0
        with self._db:
            self._db.executemany(
                """
                INSERT INTO host_samples(
                    timestamp, cpu_percent, logical_cpus, memory_total_mb,
                    memory_available_mb, memory_used_mb, memory_percent, uptime_s,
                    load_1, load_5, load_15
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def query_host_samples(
        self,
        *,
        start_timestamp: float | None = None,
        end_timestamp: float | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        limit = min(limit, 10_000)
        clauses: list[str] = []
        params: list[Any] = []
        if start_timestamp is not None:
            clauses.append("timestamp>=?")
            params.append(float(start_timestamp))
        if end_timestamp is not None:
            clauses.append("timestamp<=?")
            params.append(float(end_timestamp))
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        params.append(int(limit))
        rows = self._db.execute(
            "SELECT id, timestamp, cpu_percent, logical_cpus, memory_total_mb, "
            "memory_available_mb, memory_used_mb, memory_percent, uptime_s, "
            f"load_1, load_5, load_15 FROM host_samples {where} "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_host_samples(
        self,
        *,
        before_timestamp: float | None = None,
        max_rows: int | None = None,
    ) -> int:
        if max_rows is not None and max_rows < 0:
            raise ValueError("max_rows must be >= 0")
        deleted = 0
        with self._db:
            if before_timestamp is not None:
                cursor = self._db.execute(
                    "DELETE FROM host_samples WHERE timestamp<?", (float(before_timestamp),)
                )
                deleted += cursor.rowcount
            if max_rows is not None:
                count = int(self._db.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0])
                excess = max(0, count - int(max_rows))
                if excess:
                    cursor = self._db.execute(
                        "DELETE FROM host_samples WHERE id IN ("
                        "SELECT id FROM host_samples ORDER BY timestamp ASC, id ASC LIMIT ?)",
                        (excess,),
                    )
                    deleted += cursor.rowcount
        return deleted

    def delete_session(self, session_id: str) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        return cursor.rowcount == 1

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SQLiteTelemetryStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
