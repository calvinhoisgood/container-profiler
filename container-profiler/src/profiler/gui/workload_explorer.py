"""Asynchronous process and bounded-log explorer for one Docker container."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.workload import DockerWorkloadInspector, ProcessSnapshot


class WorkloadQueryThread(QThread):
    """Run one blocking Docker inspection call outside the GUI thread."""

    completed = pyqtSignal(str, object, object)

    def __init__(
        self,
        inspector: DockerWorkloadInspector,
        container_id: str,
        operation: str,
        *,
        log_tail: int = 200,
    ) -> None:
        super().__init__()
        if operation not in {"processes", "logs"}:
            raise ValueError("unsupported workload query operation")
        self.inspector = inspector
        self.container_id = container_id
        self.operation = operation
        self.log_tail = log_tail

    def run(self) -> None:
        try:
            if self.operation == "processes":
                payload = self.inspector.get_processes(self.container_id, max_rows=2000)
            else:
                payload = self.inspector.get_logs(
                    self.container_id,
                    tail=self.log_tail,
                    max_bytes=2 * 1024 * 1024,
                    max_lines=5000,
                )
            error = self.inspector.last_error if payload is None else None
            self.completed.emit(self.operation, payload, error)
        except Exception as exc:  # defensive: do not let a worker exception kill Qt
            self.completed.emit(self.operation, None, str(exc))


class WorkloadExplorerDialog(QDialog):
    """Explore container processes and a bounded recent-log snapshot."""

    def __init__(
        self,
        docker_client: Any,
        container_id: str,
        *,
        container_name: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.container_id = container_id
        self.inspector = DockerWorkloadInspector(docker_client)
        self.worker: WorkloadQueryThread | None = None

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

        QTimer.singleShot(0, self.refresh_processes)

    def _build_process_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.process_refresh_btn = QPushButton("刷新進程")
        self.process_refresh_btn.clicked.connect(self.refresh_processes)
        controls.addWidget(self.process_refresh_btn)
        controls.addStretch()
        layout.addLayout(controls)

        self.process_table = QTableWidget()
        self.process_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.process_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.process_table.setAlternatingRowColors(True)
        layout.addWidget(self.process_table, 1)
        self.tabs.addTab(tab, "Processes")

    def _build_log_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("最近行數:"))
        self.log_tail_spin = QSpinBox()
        self.log_tail_spin.setRange(1, 5000)
        self.log_tail_spin.setValue(200)
        controls.addWidget(self.log_tail_spin)
        self.log_refresh_btn = QPushButton("刷新日誌")
        self.log_refresh_btn.clicked.connect(self.refresh_logs)
        controls.addWidget(self.log_refresh_btn)
        controls.addStretch()
        layout.addLayout(controls)

        self.log_table = QTableWidget(0, 2)
        self.log_table.setHorizontalHeaderLabels(["Timestamp", "Message"])
        self.log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.log_table.setAlternatingRowColors(True)
        self.log_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.log_table, 1)
        self.tabs.addTab(tab, "Logs")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.process_refresh_btn.setEnabled(not busy)
        self.log_refresh_btn.setEnabled(not busy)
        self.log_tail_spin.setEnabled(not busy)
        if message:
            self.status_label.setText(message)

    def _start_query(self, operation: str) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        tail = self.log_tail_spin.value()
        worker = WorkloadQueryThread(
            self.inspector,
            self.container_id,
            operation,
            log_tail=tail,
        )
        worker.completed.connect(self._handle_result)
        worker.finished.connect(self._worker_finished)
        self.worker = worker
        self._set_busy(True, "正在讀取 Docker 資料…")
        worker.start()

    def refresh_processes(self) -> None:
        self._start_query("processes")

    def refresh_logs(self) -> None:
        self._start_query("logs")

    def _worker_finished(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.deleteLater()
        self.worker = None
        self._set_busy(False)

    def _handle_result(self, operation: str, payload: object, error: object) -> None:
        if error:
            self.status_label.setText(f"讀取失敗: {error}")
            return
        if operation == "processes":
            snapshot = payload
            if isinstance(snapshot, ProcessSnapshot):
                self._render_processes(snapshot)
                self.status_label.setText(f"進程: {len(snapshot.rows)}")
            return

        rows = tuple(payload or ())
        self.log_table.setRowCount(len(rows))
        for row_index, log_line in enumerate(rows):
            self.log_table.setItem(
                row_index, 0, QTableWidgetItem(log_line.timestamp or "")
            )
            self.log_table.setItem(row_index, 1, QTableWidgetItem(log_line.message))
        self.log_table.resizeColumnToContents(0)
        self.status_label.setText(f"日誌: {len(rows)} 行（bounded snapshot）")

    def _render_processes(self, snapshot: ProcessSnapshot) -> None:
        columns = snapshot.columns or ("Process",)
        self.process_table.clear()
        self.process_table.setColumnCount(len(columns))
        self.process_table.setHorizontalHeaderLabels(list(columns))
        self.process_table.setRowCount(len(snapshot.rows))
        for row_index, row in enumerate(snapshot.rows):
            for column_index, value in enumerate(row[: len(columns)]):
                self.process_table.setItem(
                    row_index, column_index, QTableWidgetItem(value)
                )
        self.process_table.horizontalHeader().setStretchLastSection(True)
        self.process_table.resizeColumnsToContents()

    def closeEvent(self, event: QCloseEvent) -> None:
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            # Docker SDK calls cannot always be cancelled mid-request. Avoid the
            # unsafe "QThread destroyed while running" path; keep the dialog
            # alive until the bounded one-shot call returns.
            if not worker.wait(250):
                self.status_label.setText("等待 Docker 查詢返回後關閉…")
                QTimer.singleShot(300, self.close)
                event.ignore()
                return
        event.accept()
