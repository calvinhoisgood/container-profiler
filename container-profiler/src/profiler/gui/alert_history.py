"""Bounded durable alert-event explorer."""
from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.storage import SQLiteTelemetryStore


class AlertHistoryDialog(QDialog):
    """Inspect and acknowledge the durable alert audit log."""

    MAX_ROWS = 500

    def __init__(self, store: SQLiteTelemetryStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Alert Explorer")
        self.resize(1180, 650)

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Severity:"))
        self.severity = QComboBox()
        self.severity.addItems(["全部", "critical", "warning", "info"])
        filters.addWidget(self.severity)
        filters.addWidget(QLabel("Status:"))
        self.status = QComboBox()
        self.status.addItems(["全部", "firing", "ok"])
        filters.addWidget(self.status)
        filters.addWidget(QLabel("Ack:"))
        self.ack = QComboBox()
        self.ack.addItems(["全部", "未確認", "已確認"])
        filters.addWidget(self.ack)
        self.refresh_btn = QPushButton("重新整理")
        filters.addWidget(self.refresh_btn)
        self.ack_btn = QPushButton("確認選中事件")
        filters.addWidget(self.ack_btn)
        filters.addStretch()
        layout.addLayout(filters)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels([
            "ID", "時間", "容器", "規則", "Severity", "狀態", "Metric", "Value", "Ack", "訊息"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        layout.addWidget(self.table)
        self.summary = QLabel()
        layout.addWidget(self.summary)

        self.refresh_btn.clicked.connect(self.refresh)
        self.ack_btn.clicked.connect(self.acknowledge_selected)
        self.severity.currentIndexChanged.connect(self.refresh)
        self.status.currentIndexChanged.connect(self.refresh)
        self.ack.currentIndexChanged.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _choice(combo: QComboBox) -> str | None:
        return None if combo.currentIndex() == 0 else combo.currentText()

    def refresh(self) -> None:
        ack_filter = None
        if self.ack.currentIndex() == 1:
            ack_filter = False
        elif self.ack.currentIndex() == 2:
            ack_filter = True
        try:
            rows = self.store.query_alert_events(
                severity=self._choice(self.severity),
                current_status=self._choice(self.status),
                acknowledged=ack_filter,
                limit=self.MAX_ROWS,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Alert Explorer", f"讀取告警歷史失敗: {exc}")
            return

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            timestamp = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).isoformat(timespec="seconds")
            values = (
                row["id"], timestamp, (row["container_id"] or "")[:12], row["rule_name"],
                row["severity"], row["current_status"], row["metric"],
                "-" if row["value"] is None else f'{row["value"]:g}',
                "是" if row["acknowledged_at"] else "否", row["message"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        suffix = "（已達顯示上限）" if len(rows) == self.MAX_ROWS else ""
        self.summary.setText(f"顯示 {len(rows)} 個 transition {suffix}")

    def acknowledge_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Alert Explorer", "請先選擇一個告警事件")
            return
        item = self.table.item(row, 0)
        event_id = int(item.data(Qt.ItemDataRole.UserRole))
        note, accepted = QInputDialog.getText(self, "確認告警", "備註（可留空）:")
        if not accepted:
            return
        try:
            changed = self.store.acknowledge_alert_event(event_id, note=note or None)
        except Exception as exc:
            QMessageBox.warning(self, "Alert Explorer", f"確認告警失敗: {exc}")
            return
        if not changed:
            QMessageBox.information(self, "Alert Explorer", "此事件已確認或已不存在")
        self.refresh()
