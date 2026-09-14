"""Metric cards for the latest profiling sample."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget


class MetricCard(QFrame):
    def __init__(self, title: str, unit: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setStyleSheet(
            """
            QFrame {
                background-color: #2D2D2D;
                border-radius: 5px;
                border: 1px solid #3D3D3D;
            }
            QLabel { border: none; background-color: transparent; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #AAAAAA; font-size: 12px;")
        layout.addWidget(title_label)

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            "color: #FFFFFF; font-size: 22px; font-weight: bold;"
        )
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.value_label)

        unit_label = QLabel(unit)
        unit_label.setStyleSheet("color: #AAAAAA; font-size: 12px;")
        unit_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(unit_label)

    def update_value(self, value: str) -> None:
        self.value_label.setText(value)


class MetricsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QGridLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(10)

        self.cpu_usage = MetricCard("CPU 使用率", "%")
        self.mem_usage = MetricCard("內存工作集", "MB / %")
        self.cpu_power = MetricCard("CPU 功耗", "W")
        self.gpu_power = MetricCard("GPU 功耗", "W")
        self.gpu_util = MetricCard("GPU 利用率", "%")
        self.gpu_mem = MetricCard("顯存使用", "MB")
        self.net_rx = MetricCard("網絡接收速率", "KiB/s")
        self.net_tx = MetricCard("網絡發送速率", "KiB/s")

        cards = [
            self.cpu_usage,
            self.mem_usage,
            self.cpu_power,
            self.gpu_power,
            self.gpu_util,
            self.gpu_mem,
            self.net_rx,
            self.net_tx,
        ]
        for index, card in enumerate(cards):
            layout.addWidget(card, index // 4, index % 4)

    @staticmethod
    def _number(value: Optional[float], digits: int = 1) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    def update_metrics(self, stats, power_stats) -> None:
        if stats is not None:
            self.cpu_usage.update_value(f"{stats.cpu_percent:.1f}")
            self.mem_usage.update_value(
                f"{stats.memory_mb:.0f} / {stats.memory_percent:.1f}"
            )
            rx = None if stats.network_rx_bps is None else stats.network_rx_bps / 1024.0
            tx = None if stats.network_tx_bps is None else stats.network_tx_bps / 1024.0
            self.net_rx.update_value(self._number(rx, 1))
            self.net_tx.update_value(self._number(tx, 1))

        if power_stats is not None:
            self.cpu_power.update_value(self._number(power_stats.cpu_power_w, 1))
            self.gpu_power.update_value(self._number(power_stats.gpu_power_w, 1))
            self.gpu_util.update_value(self._number(power_stats.gpu_util_percent, 1))
            if power_stats.gpu_memory_mb is None:
                self.gpu_mem.update_value("—")
            elif power_stats.gpu_memory_total_mb is None:
                self.gpu_mem.update_value(f"{power_stats.gpu_memory_mb:.0f}")
            else:
                self.gpu_mem.update_value(
                    f"{power_stats.gpu_memory_mb:.0f}/{power_stats.gpu_memory_total_mb:.0f}"
                )
