"""Asynchronous process explorer plus bounded live container logs."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.log_stream import BoundedLogBuffer, DockerLogFollower
from ..core.workload import DockerWorkloadInspector, ProcessSnapshot


class WorkloadQueryThread(QThread):
    completed = pyqtSignal(str, object, object)

    def __init__(self, inspector, container_id, operation, *, log_tail=200):
        super().__init__()
        if operation not in {"processes", "logs"}:
            raise ValueError("unsupported workload query operation")
        self.inspector = inspector
        self.container_id = container_id
        self.operation = operation
        self.log_tail = log_tail

    def run(self):
        try:
            if self.operation == "processes":
                payload = self.inspector.get_processes(self.container_id, max_rows=2000)
            else:
                payload = self.inspector.get_logs(
                    self.container_id, tail=self.log_tail,
                    max_bytes=2 * 1024 * 1024, max_lines=5000,
                )
            error = self.inspector.last_error if payload is None else None
            self.completed.emit(self.operation, payload, error)
        except Exception as exc:
            self.completed.emit(self.operation, None, str(exc))


class LogFollowThread(QThread):
    """Own one blocking Docker follow stream; the GUI drains its bounded buffer."""

    error_occurred = pyqtSignal(str)

    def __init__(self, docker_client, container_id, buffer, *, tail=100):
        super().__init__()
        self.follower = DockerLogFollower(
            docker_client, container_id, buffer, tail=tail
        )

    def run(self):
        self.follower.run(should_stop=self.isInterruptionRequested)
        if self.follower.last_error:
            self.error_occurred.emit(self.follower.last_error)

    def stop(self, timeout_ms=1000):
        self.requestInterruption()
        self.follower.stop()
        return self.wait(timeout_ms)


class WorkloadExplorerDialog(QDialog):
    """Explore container processes, snapshots, and pressure-safe live logs."""

    MAX_DISPLAY_LOG_ROWS = 5000

    def __init__(self, docker_client: Any, container_id: str, *, container_name=None, parent=None):
        super().__init__(parent)
        self.docker_client = docker_client
        self.container_id = container_id
        self.inspector = DockerWorkloadInspector(docker_client)
        self.worker: WorkloadQueryThread | None = None
        self.follow_worker: LogFollowThread | None = None
        self.follow_buffer: BoundedLogBuffer | None = None
        self.display_dropped = 0

        display_name = container_name or container_id[:12]
        self.setWindowTitle(f"Workload Explorer - {display_name}")
        self.resize(1050, 700)
        root = QVBoxLayout(self)
        title = QLabel(f"容器: {display_name}  ({container_id[:12]})")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        root.addWidget(title)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_process_tab()
        self._build_log_tab()
        self.status_label = QLabel("就緒")
        root.addWidget(self.status_label)

        self.log_drain_timer = QTimer(self)
        self.log_drain_timer.setInterval(200)
        self.log_drain_timer.timeout.connect(self._drain_follow_logs)
        QTimer.singleShot(0, self.refresh_processes)

    def _build_process_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab); controls = QHBoxLayout()
        self.process_refresh_btn = QPushButton("刷新進程")
        self.process_refresh_btn.clicked.connect(self.refresh_processes)
        controls.addWidget(self.process_refresh_btn); controls.addStretch(); layout.addLayout(controls)
        self.process_table = QTableWidget()
        self.process_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.process_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.process_table.setAlternatingRowColors(True)
        layout.addWidget(self.process_table, 1); self.tabs.addTab(tab, "Processes")

    def _build_log_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab); controls = QHBoxLayout()
        controls.addWidget(QLabel("最近行數:"))
        self.log_tail_spin = QSpinBox(); self.log_tail_spin.setRange(1, 5000); self.log_tail_spin.setValue(200)
        controls.addWidget(self.log_tail_spin)
        self.log_refresh_btn = QPushButton("刷新日誌")
        self.log_refresh_btn.clicked.connect(self.refresh_logs); controls.addWidget(self.log_refresh_btn)
        self.log_follow_btn = QPushButton("開始跟隨")
        self.log_follow_btn.clicked.connect(self.toggle_log_follow); controls.addWidget(self.log_follow_btn)
        self.log_clear_btn = QPushButton("清空")
        self.log_clear_btn.clicked.connect(self._clear_log_table); controls.addWidget(self.log_clear_btn)
        controls.addStretch(); layout.addLayout(controls)
        self.log_table = QTableWidget(0, 2)
        self.log_table.setHorizontalHeaderLabels(["Timestamp", "Message"])
        self.log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.log_table.setAlternatingRowColors(True)
        self.log_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.log_table, 1); self.tabs.addTab(tab, "Logs")

    def _set_busy(self, busy, message=""):
        following = self.follow_worker is not None and self.follow_worker.isRunning()
        self.process_refresh_btn.setEnabled(not busy)
        self.log_refresh_btn.setEnabled(not busy and not following)
        self.log_tail_spin.setEnabled(not busy and not following)
        if message: self.status_label.setText(message)

    def _start_query(self, operation):
        if self.worker is not None and self.worker.isRunning(): return
        worker = WorkloadQueryThread(
            self.inspector, self.container_id, operation, log_tail=self.log_tail_spin.value()
        )
        worker.completed.connect(self._handle_result)
        worker.finished.connect(self._worker_finished)
        self.worker = worker; self._set_busy(True, "正在讀取 Docker 資料…"); worker.start()

    def refresh_processes(self): self._start_query("processes")
    def refresh_logs(self): self._start_query("logs")

    def _worker_finished(self):
        if self.worker is not None: self.worker.deleteLater()
        self.worker = None; self._set_busy(False)

    def _handle_result(self, operation, payload, error):
        if error:
            self.status_label.setText(f"讀取失敗: {error}"); return
        if operation == "processes":
            if isinstance(payload, ProcessSnapshot):
                self._render_processes(payload); self.status_label.setText(f"進程: {len(payload.rows)}")
            return
        self._replace_log_rows(tuple(payload or ()))
        self.status_label.setText(f"日誌: {len(tuple(payload or ()))} 行（bounded snapshot）")

    def _replace_log_rows(self, rows):
        self.log_table.setRowCount(0)
        self._append_log_rows(rows)
        self.log_table.resizeColumnToContents(0)

    def _append_log_rows(self, rows):
        start = self.log_table.rowCount()
        self.log_table.setRowCount(start + len(rows))
        for offset, log_line in enumerate(rows):
            row = start + offset
            self.log_table.setItem(row, 0, QTableWidgetItem(log_line.timestamp or ""))
            self.log_table.setItem(row, 1, QTableWidgetItem(log_line.message))
        overflow = max(0, self.log_table.rowCount() - self.MAX_DISPLAY_LOG_ROWS)
        for _ in range(overflow): self.log_table.removeRow(0)
        self.display_dropped += overflow
        if rows: self.log_table.scrollToBottom()

    def _clear_log_table(self):
        self.log_table.setRowCount(0); self.display_dropped = 0

    def toggle_log_follow(self):
        if self.follow_worker is not None and self.follow_worker.isRunning():
            self._stop_log_follow(); return
        self.follow_buffer = BoundedLogBuffer(max_lines=5000, max_bytes=4 * 1024 * 1024)
        worker = LogFollowThread(
            self.docker_client, self.container_id, self.follow_buffer,
            tail=self.log_tail_spin.value(),
        )
        worker.error_occurred.connect(self._follow_error)
        worker.finished.connect(self._follow_finished)
        self.follow_worker = worker
        self.log_follow_btn.setText("停止跟隨")
        self.log_refresh_btn.setEnabled(False); self.log_tail_spin.setEnabled(False)
        self.log_drain_timer.start(); worker.start()
        self.status_label.setText("日誌跟隨中…")

    def _stop_log_follow(self):
        worker = self.follow_worker
        if worker is not None and worker.isRunning(): worker.stop()
        self.log_drain_timer.stop(); self._drain_follow_logs()
        self.log_follow_btn.setText("開始跟隨")
        self.log_refresh_btn.setEnabled(True); self.log_tail_spin.setEnabled(True)

    def _drain_follow_logs(self):
        buffer = self.follow_buffer
        if buffer is None: return
        rows = buffer.drain(250); self._append_log_rows(rows)
        stats = buffer.stats()
        fragments = self.follow_worker.follower.dropped_fragments if self.follow_worker else 0
        self.status_label.setText(
            f"live queued={stats.queued} dropped={stats.dropped} "
            f"fragments={fragments} display_evicted={self.display_dropped}"
        )

    def _follow_error(self, message): self.status_label.setText(f"日誌跟隨失敗: {message}")

    def _follow_finished(self):
        self.log_drain_timer.stop(); self._drain_follow_logs()
        worker = self.follow_worker
        if worker is not None: worker.deleteLater()
        self.follow_worker = None
        self.log_follow_btn.setText("開始跟隨")
        self.log_refresh_btn.setEnabled(True); self.log_tail_spin.setEnabled(True)

    def _render_processes(self, snapshot):
        columns = snapshot.columns or ("Process",)
        self.process_table.clear(); self.process_table.setColumnCount(len(columns))
        self.process_table.setHorizontalHeaderLabels(list(columns)); self.process_table.setRowCount(len(snapshot.rows))
        for row_index, row in enumerate(snapshot.rows):
            for column_index, value in enumerate(row[:len(columns)]):
                self.process_table.setItem(row_index, column_index, QTableWidgetItem(value))
        self.process_table.horizontalHeader().setStretchLastSection(True)
        self.process_table.resizeColumnsToContents()

    def closeEvent(self, event: QCloseEvent):
        follow = self.follow_worker
        if follow is not None and follow.isRunning():
            follow.requestInterruption(); follow.follower.stop()
            if not follow.wait(500):
                self.status_label.setText("等待 Docker 日誌串流關閉…")
                QTimer.singleShot(300, self.close); event.ignore(); return
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            if not worker.wait(250):
                self.status_label.setText("等待 Docker 查詢返回後關閉…")
                QTimer.singleShot(300, self.close); event.ignore(); return
        event.accept()
