"""Monitoring session buffer, analytics, and lossless export."""
from __future__ import annotations

import csv
import json
import math
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import ContainerStats, PowerStats
from .storage import SQLiteTelemetryStore


class DataManager:
    """Keep one monitoring session in memory and export raw + aggregate data."""

    FIELDNAMES = [
        "timestamp", "elapsed_s", "container_id", "cpu_percent", "memory_mb",
        "memory_limit_mb", "memory_percent", "network_rx_bytes", "network_tx_bytes",
        "network_rx_bps", "network_tx_bps", "pids", "cpu_power_w", "gpu_power_w",
        "gpu_util_percent", "gpu_memory_mb", "gpu_memory_total_mb", "gpu_temp_c",
    ]
    SUMMARY_METRICS = [
        "cpu_percent", "memory_mb", "memory_percent", "network_rx_bps", "network_tx_bps",
        "pids", "cpu_power_w", "gpu_power_w", "gpu_util_percent", "gpu_memory_mb", "gpu_temp_c",
    ]

    def __init__(
        self,
        store: SQLiteTelemetryStore | None = None,
        *,
        persist_batch_size: int = 100,
    ) -> None:
        if persist_batch_size <= 0:
            raise ValueError("persist_batch_size must be > 0")
        self.recorded_data: list[dict[str, Any]] = []
        self.is_recording = False
        self.start_time: float | None = None
        self.session_id: str | None = None
        self.container_id: str | None = None
        self.target_interval_ms: int | None = None
        self.store = store
        self.persist_batch_size = persist_batch_size
        self._pending_persist: list[dict[str, Any]] = []
        self._session_persist_enabled = store is not None
        self.last_storage_error: str | None = None

    @staticmethod
    def _iso_timestamp(timestamp: float | None = None) -> str:
        """Return an ISO-8601 UTC timestamp without platform-local time calls.

        Using UTC directly avoids Windows CRT failures for timestamps near the
        Unix epoch when the local timezone would shift the value before 1970.
        """
        if timestamp is None:
            value = datetime.now(timezone.utc)
        else:
            value = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return value.isoformat(timespec="milliseconds")

    def start_recording(self, container_id: str | None = None, target_interval_ms: int | None = None) -> None:
        if target_interval_ms is not None and target_interval_ms <= 0:
            raise ValueError("target_interval_ms must be > 0")
        self.recorded_data.clear()
        self.is_recording = True
        self.start_time = None
        self.session_id = str(uuid.uuid4())
        self.container_id = container_id
        self.target_interval_ms = target_interval_ms
        self._pending_persist.clear()
        self._session_persist_enabled = self.store is not None
        self.last_storage_error = None
        if self.store is not None:
            try:
                self.store.start_session(
                    self.session_id,
                    container_id=container_id,
                    started_at=self._iso_timestamp(),
                    target_interval_ms=target_interval_ms,
                )
            except Exception as exc:
                self._session_persist_enabled = False
                self.last_storage_error = str(exc)

    def _flush_persistence(self) -> None:
        if not self._pending_persist or not self._session_persist_enabled or self.store is None or self.session_id is None:
            return
        pending = self._pending_persist
        self._pending_persist = []
        try:
            self.store.append_samples(self.session_id, pending)
        except Exception as exc:
            self._session_persist_enabled = False
            self.last_storage_error = str(exc)

    def stop_recording(self) -> None:
        self.is_recording = False
        self._flush_persistence()
        if self._session_persist_enabled and self.store is not None and self.session_id is not None:
            ended_at = self.recorded_data[-1]["timestamp"] if self.recorded_data else self._iso_timestamp()
            try:
                self.store.finish_session(self.session_id, ended_at=ended_at)
            except Exception as exc:
                self.last_storage_error = str(exc)

    def add_record(self, container_id: str, stats: ContainerStats, power_stats: PowerStats) -> None:
        if not self.is_recording:
            return
        if self.container_id is None:
            self.container_id = container_id
        elif container_id != self.container_id:
            raise ValueError("a monitoring session cannot mix container IDs")

        if self.start_time is None:
            self.start_time = stats.timestamp
        elapsed = max(0.0, stats.timestamp - self.start_time)
        record = {
            "timestamp": self._iso_timestamp(stats.timestamp),
            "elapsed_s": round(elapsed, 6), "container_id": container_id,
            "cpu_percent": stats.cpu_percent, "memory_mb": stats.memory_mb,
            "memory_limit_mb": stats.memory_limit_mb, "memory_percent": stats.memory_percent,
            "network_rx_bytes": stats.network_rx_bytes, "network_tx_bytes": stats.network_tx_bytes,
            "network_rx_bps": stats.network_rx_bps, "network_tx_bps": stats.network_tx_bps,
            "pids": stats.pids, "cpu_power_w": power_stats.cpu_power_w,
            "gpu_power_w": power_stats.gpu_power_w, "gpu_util_percent": power_stats.gpu_util_percent,
            "gpu_memory_mb": power_stats.gpu_memory_mb, "gpu_memory_total_mb": power_stats.gpu_memory_total_mb,
            "gpu_temp_c": power_stats.gpu_temp_c,
        }
        self.recorded_data.append(record)
        if self._session_persist_enabled:
            self._pending_persist.append(record)
            if len(self._pending_persist) >= self.persist_batch_size:
                self._flush_persistence()

    @staticmethod
    def _numeric_values(rows: Iterable[dict[str, Any]], field: str) -> list[float]:
        values: list[float] = []
        for row in rows:
            value = row.get(field)
            if value is None or isinstance(value, bool):
                continue
            number = float(value)
            if math.isfinite(number):
                values.append(number)
        return values

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * percentile
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[lower]
        fraction = rank - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    @classmethod
    def _metric_summary(cls, rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
        values = cls._numeric_values(rows, field)
        if not values:
            return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None, "p99": None}
        return {
            "count": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values), "p50": cls._percentile(values, 0.50),
            "p95": cls._percentile(values, 0.95), "p99": cls._percentile(values, 0.99),
        }

    @staticmethod
    def _integrate_energy_wh(rows: list[dict[str, Any]], field: str) -> tuple[Optional[float], float]:
        if len(rows) < 2:
            return None, 0.0
        energy_ws = 0.0
        covered_s = 0.0
        total_s = max(0.0, float(rows[-1]["elapsed_s"]) - float(rows[0]["elapsed_s"]))
        for left, right in zip(rows, rows[1:]):
            p0, p1 = left.get(field), right.get(field)
            dt = float(right["elapsed_s"]) - float(left["elapsed_s"])
            if dt <= 0 or p0 is None or p1 is None:
                continue
            energy_ws += (float(p0) + float(p1)) * 0.5 * dt
            covered_s += dt
        if covered_s <= 0:
            return None, 0.0
        coverage = covered_s / total_s if total_s > 0 else 0.0
        return energy_ws / 3600.0, min(1.0, coverage)

    def build_summary(self) -> dict[str, Any]:
        rows = self.recorded_data
        duration_s = float(rows[-1]["elapsed_s"]) if rows else 0.0
        intervals_ms = [
            (float(b["elapsed_s"]) - float(a["elapsed_s"])) * 1000.0
            for a, b in zip(rows, rows[1:])
            if float(b["elapsed_s"]) >= float(a["elapsed_s"])
        ]
        estimated_missed = 0
        if self.target_interval_ms and self.target_interval_ms > 0:
            for interval in intervals_ms:
                if interval > self.target_interval_ms * 1.5:
                    estimated_missed += max(0, round(interval / self.target_interval_ms) - 1)

        cpu_wh, cpu_cov = self._integrate_energy_wh(rows, "cpu_power_w")
        gpu_wh, gpu_cov = self._integrate_energy_wh(rows, "gpu_power_w")
        total_wh = None if cpu_wh is None and gpu_wh is None else (cpu_wh or 0.0) + (gpu_wh or 0.0)

        return {
            "schema_version": 1,
            "session": {
                "session_id": self.session_id, "container_id": self.container_id,
                "sample_count": len(rows), "duration_s": duration_s,
                "target_interval_ms": self.target_interval_ms,
                "started_at": rows[0]["timestamp"] if rows else None,
                "ended_at": rows[-1]["timestamp"] if rows else None,
            },
            "sampling": {
                "interval_count": len(intervals_ms),
                "mean_interval_ms": statistics.fmean(intervals_ms) if intervals_ms else None,
                "p95_interval_ms": self._percentile(intervals_ms, 0.95),
                "max_interval_ms": max(intervals_ms) if intervals_ms else None,
                "estimated_missed_samples": estimated_missed,
            },
            "metrics": {field: self._metric_summary(rows, field) for field in self.SUMMARY_METRICS},
            "energy": {
                "cpu_wh": cpu_wh, "gpu_wh": gpu_wh, "total_wh": total_wh,
                "cpu_coverage_ratio": cpu_cov, "gpu_coverage_ratio": gpu_cov,
            },
        }

    def export_csv(self, file_path: str | Path) -> bool:
        if not self.recorded_data:
            return False
        try:
            path = Path(file_path); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES); writer.writeheader(); writer.writerows(self.recorded_data)
            return True
        except (OSError, csv.Error) as exc:
            print(f"導出 CSV 失敗: {exc}"); return False

    def close(self) -> None:
        if self.is_recording:
            self.stop_recording()
        if self.store is not None:
            self.store.close()

    def export_summary_json(self, file_path: str | Path) -> bool:
        if not self.recorded_data:
            return False
        try:
            path = Path(file_path); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(self.build_summary(), handle, ensure_ascii=False, indent=2)
            return True
        except OSError as exc:
            print(f"導出摘要失敗: {exc}"); return False
