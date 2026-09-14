"""Main window and background sampling worker."""
from __future__ import annotations

import time
from datetime import datetime

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.data_manager import DataManager
from ..core.docker_monitor import DockerMonitor
from ..core.power_monitor import PowerMonitor
from ..utils.paths import get_output_dir
from .chart_widget import ChartWidget
from .container_list import ContainerListWidget
from .metrics_panel import MetricsPanel


class WorkerThread(QThread):
    """Collect blocking Docker/sensor data outside the GUI thread."""

    data_ready = pyqtSignal(str, object, object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        docker_monitor: DockerMonitor,
        power_monitor: PowerMonitor,
        container_id: str,
        interval_ms: int,
    ) -> None:
        super().__init__()
        self.docker_monitor = docker_monitor
        self.power_monitor = power_monitor
        self.container_id = container_id
        self.interval_ms = interval_ms

    def run(self) -> None:
        while not self.isInterruptionRequested():
            started = time.monotonic()
            stats = self.docker_monitor.get_stats(self.container_id)
            if stats is None:
                detail = self.docker_monitor.last_error or "容器可能已停止"
                self.error_occurred.emit(f"獲取容器數據失敗: {detail}")
                return

            power_stats = self.power_monitor.get_power_stats()
            self.data_ready.emit(self.container_id, stats, power_stats)

            elapsed_ms = (time.monotonic() - started) * 1000.0
            remaining_ms = max(0, int(self.interval_ms - elapsed_ms))
            while remaining_ms > 0 and not self.isInterruptionRequested():
                chunk = min(50, remaining_ms)
                self.msleep(chunk)
                remaining_ms -= chunk

    def stop(self, timeout_ms: int = 3000) -> bool:
        self.requestInterruption()
        return self.wait(timeout_ms)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Container Profiler v5")
        self.setGeometry(100, 100, 1400, 900)
        self.setMinimumSize(1000, 600)

        self.docker_monitor = DockerMonitor()
        self.power_monitor = PowerMonitor()
        self.data_manager = DataManager()

        self.selected_container_id: str | None = None
        self.monitoring_container_id: str | None = None
        self.worker_thread: WorkerThread | None = None

        self._setup_ui()
        self._setup_style()
        self._setup_statusbar()

        self.container_list.refresh_requested.connect(self._refresh_container_list)
        self.container_list.container_selected.connect(self._on_container_selected)
        self.start_btn.clicked.connect(self._toggle_monitoring)
        self.export_btn.clicked.connect(self._export_data)
        self._refresh_container_list()

    @property
    def is_monitoring(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.isRunning()

    def _refresh_container_list(self) -> None:
        containers = self.docker_monitor.list_containers()
        self.container_list.update_list(containers)
        if not containers:
            detail = self.docker_monitor.last_error or "未發現容器"
            self.statusBar().showMessage(f"Docker: {detail}")

    def _on_container_selected(self, container_id: str) -> None:
        self.selected_container_id = container_id
        self.statusBar().showMessage(f"已選擇容器: {container_id[:12]}")

    def _toggle_monitoring(self, checked: bool) -> None:
        if checked:
            self._start_monitoring()
        else:
            self._stop_monitoring()

    def _start_monitoring(self) -> None:
        if not self.selected_container_id:
            QMessageBox.warning(self, "警告", "請先選擇一個容器")
            self.start_btn.setChecked(False)
            return

        self.monitoring_container_id = self.selected_container_id
        self.docker_monitor.reset_container_baseline(self.monitoring_container_id)
        self.data_manager.start_recording()
        self.chart_widget.reset()

        interval = self.interval_spin.value()
        worker = WorkerThread(
            self.docker_monitor,
            self.power_monitor,
            self.monitoring_container_id,
            interval,
        )
        worker.data_ready.connect(self._handle_new_data)
        worker.error_occurred.connect(self._handle_monitor_error)
        self.worker_thread = worker
        worker.start()

        self.start_btn.setText("停止監控")
        self.export_btn.setEnabled(False)
        self.interval_spin.setEnabled(False)
        self.container_list.setEnabled(False)
        self.statusBar().showMessage(
            f"正在監控 {self.monitoring_container_id[:12]} | 目標採樣週期 {interval} ms"
        )

    def _stop_monitoring(self, status_message: str | None = None) -> None:
        worker = self.worker_thread
        self.worker_thread = None
        if worker is not None and worker.isRunning():
            if not worker.stop():
                status_message = "採樣線程仍在等待底層調用返回，稍後再退出"

        self.data_manager.stop_recording()
        self.start_btn.setChecked(False)
        self.start_btn.setText("開始監控")
        self.export_btn.setEnabled(bool(self.data_manager.recorded_data))
        self.interval_spin.setEnabled(True)
        self.container_list.setEnabled(True)
        self.monitoring_container_id = None
        if status_message is None:
            status_message = f"監控已停止 | 已記錄 {len(self.data_manager.recorded_data)} 條數據"
        self.statusBar().showMessage(status_message)

    def _handle_new_data(self, container_id: str, stats, power_stats) -> None:
        if container_id != self.monitoring_container_id:
            return
        self.data_manager.add_record(container_id, stats, power_stats)
        self.metrics_panel.update_metrics(stats, power_stats)
        self.chart_widget.update_data(stats, power_stats)

    def _handle_monitor_error(self, error_msg: str) -> None:
        self._stop_monitoring(error_msg)

    def _export_data(self) -> None:
        if not self.data_manager.recorded_data:
            QMessageBox.information(self, "提示", "暫無數據可導出")
            return

        default_name = f"profiler_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        default_path = get_output_dir() / default_name
        file_path, _ = QFileDialog.getSaveFileName(
            self, "導出 CSV", str(default_path), "CSV Files (*.csv)"
        )
        if not file_path:
            return
        if self.data_manager.export_csv(file_path):
            QMessageBox.information(self, "成功", f"數據已導出至:\n{file_path}")
        else:
            QMessageBox.critical(self, "錯誤", "導出失敗")

    def _setup_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.container_list = ContainerListWidget()
        splitter.addWidget(self.container_list)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 0, 0, 0)
        control_layout = QHBoxLayout()

        self.start_btn = QPushButton("開始監控")
        self.start_btn.setCheckable(True)
        self.start_btn.setStyleSheet(
            """
            QPushButton { background-color: #007ACC; border: none; padding: 8px 16px; border-radius: 4px; }
            QPushButton:checked { background-color: #DA3633; }
            QPushButton:hover { background-color: #1E8AD6; }
            """
        )

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 5000)
        self.interval_spin.setSingleStep(20)
        self.interval_spin.setValue(1000)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setFixedWidth(110)

        self.export_btn = QPushButton("導出數據")
        self.export_btn.setEnabled(False)
        self.export_btn.setStyleSheet(
            """
            QPushButton { background-color: #3D3D3D; border: 1px solid #555555; padding: 8px 16px; border-radius: 4px; }
            QPushButton:hover { background-color: #4D4D4D; }
            QPushButton:disabled { color: #777777; }
            """
        )

        control_layout.addWidget(self.start_btn)
        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("目標週期:"))
        control_layout.addWidget(self.interval_spin)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.export_btn)
        control_layout.addStretch()
        right_layout.addLayout(control_layout)

        self.chart_widget = ChartWidget()
        right_layout.addWidget(self.chart_widget, stretch=2)
        self.metrics_panel = MetricsPanel()
        right_layout.addWidget(self.metrics_panel, stretch=1)

        splitter.addWidget(right_panel)
        splitter.setSizes([300, 1100])
        main_layout.addWidget(splitter)

    def _setup_statusbar(self) -> None:
        docker = "已連接" if self.docker_monitor.is_connected() else "未連接"
        nvml = "GPU已就緒" if self.power_monitor.nvml.is_available() else "GPU不可用"
        hwinfo = "HWiNFO已就緒" if self.power_monitor.hwinfo.is_connected() else "HWiNFO不可用"
        self.statusBar().showMessage(f"Docker: {docker} | {nvml} | {hwinfo}")
        self.statusBar().setStyleSheet("color: #FFFFFF; background-color: #007ACC;")

    def _setup_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background-color: #1E1E1E; }
            QWidget { color: #FFFFFF; font-family: 'Segoe UI', Arial; }
            QSplitter::handle { background-color: #3D3D3D; }
            QListWidget { background-color: #252526; border: 1px solid #3D3D3D; border-radius: 4px; }
            QListWidget::item:selected { background-color: #094771; color: #FFFFFF; }
            QComboBox, QSpinBox { background-color: #2D2D2D; color: #FFFFFF; border: 1px solid #3D3D3D; border-radius: 4px; padding: 4px; }
            QComboBox QAbstractItemView { background-color: #2D2D2D; color: #FFFFFF; selection-background-color: #094771; }
            QLabel { color: #FFFFFF; }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker_thread is not None:
            self._stop_monitoring("正在退出")
        self.power_monitor.close()
        event.accept()
