"""Read-only explorer for persisted monitoring sessions and samples."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.storage import SQLiteTelemetryStore


class SessionSamplesDialog(QDialog):
    """Explorer for raw samples in one persisted monitoring session."""

    COLUMNS = (
        ("timestamp", "時間"),
        ("elapsed_s", "Elapsed s"),
        ("cpu_percent", "CPU %"),
        ("memory_mb", "Memory MiB"),
        ("memory_percent", "Memory %"),
        ("network_rx_bps", "RX B/s"),
        ("network_tx_bps", "TX B/s"),
        ("cpu_power_w", "CPU W"),
        ("gpu_power_w", "GPU W"),
        ("gpu_util_percent", "GPU %"),
        ("gpu_temp_c", "GPU °C"),
        ("pids", "PIDs"),
    )

    def __init__(self, store: SQLiteTelemetryStore, session_id: str, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.session_id = session_id
        self.setWindowTitle(f"Session 樣本 - {session_id[:12]}")
        self.resize(1200, 620)

        layout = QVBoxLayout(self)
        self.info_label = QLabel()
        layout.addWidget(self.info_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _, label in self.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        footer.addStretch()
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        self.refresh()

    @staticmethod
    def _format_value(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    def refresh(self) -> None:
        # Bound the GUI query so an accidentally long high-frequency session
        # cannot freeze the desktop. A richer paginated explorer can build on
        # the same store API later.
        rows = self.store.query_samples(self.session_id, limit=5000)
        self.table.setRowCount(len(rows))
        for row_index, sample in enumerate(rows):
            for column, (field, _) in enumerate(self.COLUMNS):
                item = QTableWidgetItem(self._format_value(sample.get(field)))
                if field != "timestamp":
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        suffix = "（最多顯示 5000 筆）" if len(rows) == 5000 else ""
        self.info_label.setText(f"Session {self.session_id} | {len(rows)} 筆樣本 {suffix}")


class SessionHistoryDialog(QDialog):
    """History browser backed by the shared SQLite telemetry store."""

    HEADERS = ("開始時間", "結束時間", "容器", "樣本", "目標週期", "Session ID")

    def __init__(self, store: SQLiteTelemetryStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("監控歷史")
        self.resize(980, 520)

        layout = QVBoxLayout(self)
        hint = QLabel("本機持久化監控 Session。雙擊任一列可查看原始樣本；資料不依賴 CSV 匯出。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self.open_selected())
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        view_btn = QPushButton("查看樣本")
        view_btn.clicked.connect(self.open_selected)
        refresh_btn = QPushButton("重新整理")
        refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(self.status_label)
        footer.addStretch()
        footer.addWidget(view_btn)
        footer.addWidget(refresh_btn)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self.refresh()

    @staticmethod
    def _text(value: Any, fallback: str = "-") -> str:
        if value is None or value == "":
            return fallback
        return str(value)

    def selected_session_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 5)
        return item.text() if item is not None else None

    def open_selected(self) -> None:
        session_id = self.selected_session_id()
        if not session_id:
            QMessageBox.information(self, "提示", "請先選擇一個 Session")
            return
        SessionSamplesDialog(self.store, session_id, self).exec()

    def refresh(self) -> None:
        sessions = self.store.list_sessions(limit=500)
        self.table.setRowCount(len(sessions))
        for row_index, session in enumerate(sessions):
            interval = session.get("target_interval_ms")
            values = (
                self._text(session.get("started_at")),
                self._text(session.get("ended_at"), "進行中/未正常結束"),
                self._text(session.get("container_id")),
                self._text(session.get("sample_count"), "0"),
                f"{interval} ms" if interval is not None else "-",
                self._text(session.get("session_id")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row_index, column, item)

        self.table.resizeColumnsToContents()
        self.status_label.setText(f"共 {len(sessions)} 個 Session")
