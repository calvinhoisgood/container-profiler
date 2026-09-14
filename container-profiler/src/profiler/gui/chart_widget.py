"""Real-time charts with unit-consistent display modes."""
from __future__ import annotations

import time
from collections import deque

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class ChartWidget(QWidget):
    MODES = ["CPU & Memory (%)", "Power (W)", "GPU (%)", "Network (MiB/s)"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.history_size = 3000
        self.timestamps: deque[float] = deque(maxlen=self.history_size)
        self.data_series = {
            "cpu": deque(maxlen=self.history_size),
            "memory": deque(maxlen=self.history_size),
            "power": deque(maxlen=self.history_size),
            "gpu": deque(maxlen=self.history_size),
            "rx": deque(maxlen=self.history_size),
            "tx": deque(maxlen=self.history_size),
        }
        self.curves: dict[str, object] = {}
        self.legend = None
        self._setup_ui()
        self._update_chart_mode(0)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("實時監控曲線"))

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(self.MODES)
        self.mode_combo.currentIndexChanged.connect(self._update_chart_mode)
        self.mode_combo.setFixedWidth(170)
        toolbar.addWidget(self.mode_combo)

        self.auto_scale_cb = QCheckBox("自動縮放")
        self.auto_scale_cb.setChecked(True)
        self.auto_scale_cb.stateChanged.connect(self._toggle_auto_scale)
        toolbar.addWidget(self.auto_scale_cb)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#000000")
        font = QFont()
        font.setPixelSize(12)
        for axis_name in ("bottom", "left"):
            axis = self.plot_widget.getAxis(axis_name)
            axis.setTickFont(font)
            axis.setPen("#DDDDDD")
            axis.setTextPen("#DDDDDD")
        self.plot_widget.setLabel("bottom", "Time", units="s")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot_widget)

    def reset(self) -> None:
        self.timestamps.clear()
        for series in self.data_series.values():
            series.clear()
        self._refresh_curves()

    def _toggle_auto_scale(self, state: int) -> None:
        enabled = state == Qt.CheckState.Checked.value
        self.plot_widget.enableAutoRange(axis="y", enable=enabled)
        if not enabled:
            self._apply_default_range(self.mode_combo.currentIndex())

    def _remove_legend(self) -> None:
        if self.legend is not None:
            try:
                self.legend.scene().removeItem(self.legend)
            except Exception:
                pass
            self.legend = None

    def _update_chart_mode(self, index: int) -> None:
        self.plot_widget.clear()
        self._remove_legend()
        self.legend = self.plot_widget.addLegend()
        self.curves = {}

        if index == 0:
            self.plot_widget.setLabel("left", "Utilization", units="%")
            self.curves["cpu"] = self.plot_widget.plot(
                name="Container CPU %", pen=pg.mkPen("#2196F3", width=2)
            )
            self.curves["memory"] = self.plot_widget.plot(
                name="Memory %", pen=pg.mkPen("#4CAF50", width=2)
            )
        elif index == 1:
            self.plot_widget.setLabel("left", "Power", units="W")
            self.curves["power"] = self.plot_widget.plot(
                name="CPU + GPU", pen=pg.mkPen("#FFC107", width=2)
            )
        elif index == 2:
            self.plot_widget.setLabel("left", "GPU Utilization", units="%")
            self.curves["gpu"] = self.plot_widget.plot(
                name="GPU %", pen=pg.mkPen("#9C27B0", width=2)
            )
        else:
            self.plot_widget.setLabel("left", "Network", units="MiB/s")
            self.curves["rx"] = self.plot_widget.plot(
                name="RX", pen=pg.mkPen("#00BCD4", width=2)
            )
            self.curves["tx"] = self.plot_widget.plot(
                name="TX", pen=pg.mkPen("#FF7043", width=2)
            )

        auto_scale = self.auto_scale_cb.isChecked()
        self.plot_widget.enableAutoRange(axis="y", enable=auto_scale)
        if not auto_scale:
            self._apply_default_range(index)
        self._refresh_curves()

    def _apply_default_range(self, index: int) -> None:
        if index in (0, 2):
            self.plot_widget.setYRange(0, 100)
        else:
            self.plot_widget.enableAutoRange(axis="y", enable=True)

    def update_data(self, stats, power_stats) -> None:
        now = time.monotonic()
        self.timestamps.append(now)
        self.data_series["cpu"].append(float(stats.cpu_percent))
        self.data_series["memory"].append(float(stats.memory_percent))
        self.data_series["power"].append(
            float((power_stats.cpu_power_w or 0.0) + (power_stats.gpu_power_w or 0.0))
        )
        self.data_series["gpu"].append(float(power_stats.gpu_util_percent or 0.0))
        self.data_series["rx"].append(float(stats.network_rx_bps or 0.0) / 1024.0 / 1024.0)
        self.data_series["tx"].append(float(stats.network_tx_bps or 0.0) / 1024.0 / 1024.0)
        self._refresh_curves()

    def _refresh_curves(self) -> None:
        if not self.timestamps:
            x_axis: list[float] = []
        else:
            latest = self.timestamps[-1]
            x_axis = [timestamp - latest for timestamp in self.timestamps]
        for key, curve in self.curves.items():
            curve.setData(x_axis, list(self.data_series[key]))
