"""Bounded local Explorer for persisted custom and system metrics."""
from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class CustomMetricsExplorerDialog(QDialog):
    """Query recent metric points without ever loading the whole local store."""

    MAX_QUERY_ROWS = 5000

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Metrics Explorer")
        self.resize(1200, 720)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()

        filters.addWidget(QLabel("Metric prefix:"))
        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("例如 system.net. / demo. / http.requests")
        self.name_filter.returnPressed.connect(self.refresh)
        filters.addWidget(self.name_filter, 2)

        filters.addWidget(QLabel("Container:"))
        self.container_filter = QLineEdit()
        self.container_filter.setPlaceholderText("完整 container id，可留空")
        self.container_filter.returnPressed.connect(self.refresh)
        filters.addWidget(self.container_filter, 2)

        filters.addWidget(QLabel("Source:"))
        self.source_filter = QComboBox()
        self.source_filter.addItems(("all", "system", "openmetrics", "statsd"))
        filters.addWidget(self.source_filter)

        filters.addWidget(QLabel("Rows:"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(50, self.MAX_QUERY_ROWS)
        self.limit_spin.setSingleStep(50)
        self.limit_spin.setValue(500)
        filters.addWidget(self.limit_spin)

        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        filters.addWidget(refresh)
        layout.addLayout(filters)

        self.summary = QLabel()
        layout.addWidget(self.summary)

        headers = (
            "Time (UTC)", "Metric", "Value", "Type", "Unit", "Tags",
            "Container", "Source", "Target",
        )
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

    @staticmethod
    def _timestamp_text(value) -> str:
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat(
                timespec="milliseconds"
            )
        except (TypeError, ValueError, OSError):
            return str(value or "")

    def refresh(self) -> None:
        prefix = self.name_filter.text().strip()
        container = self.container_filter.text().strip() or None
        source_text = self.source_filter.currentText()
        source = None if source_text == "all" else source_text
        rows = self.store.query_custom_metrics(
            name_prefix=prefix or None,
            container_id=container,
            source=source,
            limit=min(self.limit_spin.value(), self.MAX_QUERY_ROWS),
        )

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                self._timestamp_text(row.get("timestamp")),
                row.get("name") or "",
                f"{row.get('value', 0):g}",
                row.get("metric_type") or "",
                row.get("unit") or "",
                ", ".join(row.get("tags") or ()),
                row.get("container_id") or "",
                row.get("source") or "",
                row.get("target_key") or "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row_index, column, item)

        self.table.resizeColumnsToContents()
        self.summary.setText(
            f"顯示 {len(rows)} 筆（查詢上限 {self.limit_spin.value()}；資料庫查詢硬上限 "
            f"{self.MAX_QUERY_ROWS}）"
        )
