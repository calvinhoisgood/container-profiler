"""Durable local telemetry store backed by SQLite.

The store is intentionally independent from Qt and Docker so it can be used by
future agent/service and GUI processes. WAL mode allows a writer and explorers
to coexist without making the sampling path depend on the UI.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping


class SQLiteTelemetryStore:
    SCHEMA_VERSION = 1

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
