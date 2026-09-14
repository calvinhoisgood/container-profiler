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
from ..core.self_telemetry import AgentSelfTelemetry, advance_fixed_rate_deadline
from ..core.storage import SQLiteTelemetryStore
from ..utils.paths import get_output_dir, get_telemetry_db_path
from .chart_widget import ChartWidget
from .container_list import ContainerListWidget
from .metrics_panel import MetricsPanel
from .session_history import SessionHistoryDialog


class WorkerThread(QThread):
    """Collect blocking Docker/sensor data outside the GUI thread."""

    data_ready = pyqtSignal(str, object, object)
    health_ready = pyqtSignal(object)
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
        self.telemetry = AgentSelfTelemetry(interval_s=interval_ms / 1000.0)

    def _sleep_until(self, deadline: float) -> None:
        while not self.isInterruptionRequested():
            remaining_ms = int((deadline - time.monotonic()) * 1000.0)
            if remaining_ms <= 0:
                return
            self.msleep(min(50, remaining_ms))

    def run(self) -> None:
        interval_s = self.interval_ms / 1000.0
        deadline = time.monotonic()
        cycle_count = 0

        while not self.isInterruptionRequested():
            started = time.monotonic()
            stats = self.docker_monitor.get_stats(self.container_id)
            power_stats = self.power_monitor.get_power_stats() if stats is not None else None
            finished = time.monotonic()

            self.telemetry.record_cycle(
                scheduled_at=deadline,
                started_at=started,
                finished_at=finished,
                success=stats is not None,
            )
            cycle_count += 1

            if stats is None:
                self.health_ready.emit(self.telemetry.snapshot())
                detail = self.docker_monitor.last_error or "容器可能已停止"
                self.error_occurred.emit(f"獲取容器數據失敗: {detail}")
                return

            self.data_ready.emit(self.container_id, stats, power_stats)

            deadline, skipped = advance_fixed_rate_deadline(deadline, finished, interval_s)
            if skipped:
                self.telemetry.record_skipped_ticks(skipped)

            if skipped or cycle_count % 10 == 0:
                self.health_ready.emit(self.telemetry.snapshot())

            self._sleep_until(deadline)

        self.health_ready.emit(self.telemetry.snapshot())

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
        self.telemetry_store: SQLiteTelemetryStore | None = None
        self.storage_init_error: str | None = None
        try:
            self.telemetry_store = SQLiteTelemetryStore(get_telemetry_db_path())
        except Exception as exc:
            self.storage_init_error = str(exc)
        self.data_manager = DataManager(store=self.telemetry_store)

        self.selected_container_id: str | None = None
        self.monitoring_container_id: str | None = None
        self.worker_thread: WorkerThread | None = None
        self.last_health_snapshot = None

        self._setup_ui()
        self._setup_style()
        self._setup_statusbar()

        self.container_list.refresh_requested.connect(self._refresh_container_list)
        self.container_list.container_selected.connect(self._on_container_selected)
        self.start_btn.clicked.connect(self._toggle_monitoring)
        self.export_btn.clicked.connect(self._export_data)
        self.history_btn.clicked.connect(self._show_history)
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
        interval = self.interval_spin.value()
        self.data_manager.start_recording(
            container_id=self.monitoring_container_id,
            target_interval_ms=interval,
        )
        self.chart_widget.reset()
        self.last_health_snapshot = None

        worker = WorkerThread(
            self.docker_monitor,
            self.power_monitor,
            self.monitoring_container_id,
            interval,
        )
        worker.data_ready.connect(self._handle_new_data)
        worker.health_ready.connect(self._handle_health)
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
        if worker is not None:
            self.last_health_snapshot = worker.telemetry.snapshot()

        self.data_manager.stop_recording()
        self.start_btn.setChecked(False)
        self.start_btn.setText("開始監控")
        self.export_btn.setEnabled(bool(self.data_manager.recorded_data))
        self.interval_spin.setEnabled(True)
        self.container_list.setEnabled(True)
        self.monitoring_container_id = None
        if status_message is None:
            status_message = f"監控已停止 | 已記錄 {len(self.data_manager.recorded_data)} 條數據"
            if self.last_health_snapshot is not None:
                status_message += f" | skipped {self.last_health_snapshot.skipped_ticks}"
            if self.data_manager.last_storage_error:
                status_message += " | 本機持久化失敗"
        self.statusBar().showMessage(status_message)

    def _handle_new_data(self, container_id: str, stats, power_stats) -> None:
        if container_id != self.monitoring_container_id:
            return
        self.data_manager.add_record(container_id, stats, power_stats)
        self.metrics_panel.update_metrics(stats, power_stats)
        self.chart_widget.update_data(stats, power_stats)

    def _handle_health(self, snapshot) -> None:
        self.last_health_snapshot = snapshot
        if not self.monitoring_container_id:
            return
        latency = snapshot.collection_latency_ms_p95
        lag = snapshot.scheduling_lag_ms_p95
        latency_text = "-" if latency is None else f"{latency:.1f}ms"
        lag_text = "-" if lag is None else f"{lag:.1f}ms"
        storage_text = ""
        if self.data_manager.last_storage_error:
            storage_text = " | storage degraded"
        self.statusBar().showMessage(
            f"監控 {self.monitoring_container_id[:12]} | collect p95 {latency_text} | "
            f"lag p95 {lag_text} | skipped {snapshot.skipped_ticks} | "
            f"failures {snapshot.failed_cycles}{storage_text}"
        )

    def _handle_monitor_error(self, error_msg: str) -> None:
        self._stop_monitoring(error_msg)

    def _show_history(self) -> None:
        if self.telemetry_store is None:
            detail = self.storage_init_error or "本機資料庫不可用"
            QMessageBox.warning(self, "監控歷史不可用", detail)
            return
        dialog = SessionHistoryDialog(self.telemetry_store, self)
        dialog.exec()

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
            summary_path = f"{file_path}.summary.json"
            self.data_manager.export_summary_json(summary_path)
            QMessageBox.information(
                self,
                "成功",
                f"數據已導出至:\n{file_path}\n摘要:\n{summary_path}",
            )
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

        self.history_btn = QPushButton("監控歷史")
        self.history_btn.setEnabled(self.telemetry_store is not None)
        self.history_btn.setToolTip(str(get_telemetry_db_path()))

        control_layout.addWidget(self.start_btn)
        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("目標週期:"))
        control_layout.addWidget(self.interval_spin)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.export_btn)
        control_layout.addWidget(self.history_btn)
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
        storage = "SQLite已就緒" if self.telemetry_store is not None else "SQLite不可用"
        self.statusBar().showMessage(f"Docker: {docker} | {nvml} | {hwinfo} | {storage}")
        self.statusBar().setStyleSheet("color: #FFFFFF; background-color: #007ACC;")

    def _setup_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background-color: #1E1E1E; }
            QWidget { color: #FFFFFF; font-family: 'Segoe UI', Arial; }
            QSplitter::handle { background-color: #3D3D3D; }
            QListWidget, QTableWidget { background-color: #252526; border: 1px solid #3D3D3D; border-radius: 4px; }
            QListWidget::item:selected, QTableWidget::item:selected { background-color: #094771; color: #FFFFFF; }
            QComboBox, QSpinBox { background-color: #2D2D2D; color: #FFFFFF; border: 1px solid #3D3D3D; border-radius: 4px; padding: 4px; }
            QComboBox QAbstractItemView { background-color: #2D2D2D; color: #FFFFFF; selection-background-color: #094771; }
            QLabel { color: #FFFFFF; }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker_thread is not None:
            self._stop_monitoring("正在退出")
        self.data_manager.close()
        self.power_monitor.close()
        event.accept()
