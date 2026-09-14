"""Read-only explorer for persisted monitoring sessions."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.storage import SQLiteTelemetryStore


class SessionHistoryDialog(QDialog):
    """Small history browser backed by the shared SQLite telemetry store."""

    HEADERS = ("開始時間", "結束時間", "容器", "樣本", "目標週期", "Session ID")

    def __init__(self, store: SQLiteTelemetryStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("監控歷史")
        self.resize(980, 520)

        layout = QVBoxLayout(self)
        hint = QLabel("本機持久化監控 Session。資料儲存在使用者目錄，不依賴 CSV 匯出。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        refresh_btn = QPushButton("重新整理")
        refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(self.status_label)
        footer.addStretch()
        footer.addWidget(refresh_btn)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self.refresh()

    @staticmethod
    def _text(value: Any, fallback: str = "-") -> str:
        if value is None or value == "":
            return fallback
        return str(value)

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
