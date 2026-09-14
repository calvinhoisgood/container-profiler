"""Recording buffer and CSV export."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ContainerStats, PowerStats


class DataManager:
    """Keep one monitoring session in memory and export it losslessly."""

    FIELDNAMES = [
        "timestamp",
        "elapsed_s",
        "container_id",
        "cpu_percent",
        "memory_mb",
        "memory_limit_mb",
        "memory_percent",
        "network_rx_bytes",
        "network_tx_bytes",
        "network_rx_bps",
        "network_tx_bps",
        "pids",
        "cpu_power_w",
        "gpu_power_w",
        "gpu_util_percent",
        "gpu_memory_mb",
        "gpu_memory_total_mb",
        "gpu_temp_c",
    ]

    def __init__(self) -> None:
        self.recorded_data: list[dict[str, Any]] = []
        self.is_recording = False
        self.start_time: float | None = None

    def start_recording(self) -> None:
        self.recorded_data.clear()
        self.is_recording = True
        self.start_time = None

    def stop_recording(self) -> None:
        self.is_recording = False

    def add_record(
        self, container_id: str, stats: ContainerStats, power_stats: PowerStats
    ) -> None:
        if not self.is_recording:
            return

        if self.start_time is None:
            self.start_time = stats.timestamp
        elapsed = max(0.0, stats.timestamp - self.start_time)

        self.recorded_data.append(
            {
                "timestamp": datetime.fromtimestamp(stats.timestamp)
                .astimezone()
                .isoformat(timespec="milliseconds"),
                "elapsed_s": round(elapsed, 3),
                "container_id": container_id,
                "cpu_percent": stats.cpu_percent,
                "memory_mb": stats.memory_mb,
                "memory_limit_mb": stats.memory_limit_mb,
                "memory_percent": stats.memory_percent,
                "network_rx_bytes": stats.network_rx_bytes,
                "network_tx_bytes": stats.network_tx_bytes,
                "network_rx_bps": stats.network_rx_bps,
                "network_tx_bps": stats.network_tx_bps,
                "pids": stats.pids,
                "cpu_power_w": power_stats.cpu_power_w,
                "gpu_power_w": power_stats.gpu_power_w,
                "gpu_util_percent": power_stats.gpu_util_percent,
                "gpu_memory_mb": power_stats.gpu_memory_mb,
                "gpu_memory_total_mb": power_stats.gpu_memory_total_mb,
                "gpu_temp_c": power_stats.gpu_temp_c,
            }
        )

    def export_csv(self, file_path: str | Path) -> bool:
        if not self.recorded_data:
            return False

        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
                writer.writeheader()
                writer.writerows(self.recorded_data)
            return True
        except (OSError, csv.Error) as exc:
            print(f"導出 CSV 失敗: {exc}")
            return False
