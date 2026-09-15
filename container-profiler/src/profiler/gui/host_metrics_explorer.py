"""Bounded Explorer for native host telemetry."""
from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class HostMetricsExplorerDialog(QDialog):
    MAX_QUERY_ROWS = 5000

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Native Host Metrics")
        self.resize(1150, 700)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Rows:"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(50, self.MAX_QUERY_ROWS)
        self.limit_spin.setSingleStep(50)
        self.limit_spin.setValue(500)
        controls.addWidget(self.limit_spin)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        controls.addStretch()
        layout.addLayout(controls)

        self.summary = QLabel()
        layout.addWidget(self.summary)
        headers = (
            "Time (UTC)", "CPU %", "Logical CPUs", "Memory %", "Used MiB",
            "Available MiB", "Total MiB", "Uptime s", "Load 1", "Load 5", "Load 15",
        )
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

    @staticmethod
    def _fmt(value, digits: int = 2) -> str:
        return "-" if value is None else f"{float(value):.{digits}f}"

    def refresh(self) -> None:
        rows = self.store.query_host_samples(limit=self.limit_spin.value())
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            try:
                when = datetime.fromtimestamp(
                    float(row["timestamp"]), timezone.utc
                ).isoformat(timespec="milliseconds")
            except (TypeError, ValueError, OSError):
                when = str(row.get("timestamp") or "")
            values = (
                when,
                self._fmt(row.get("cpu_percent")),
                str(row.get("logical_cpus") or ""),
                self._fmt(row.get("memory_percent")),
                self._fmt(row.get("memory_used_mb")),
                self._fmt(row.get("memory_available_mb")),
                self._fmt(row.get("memory_total_mb")),
                self._fmt(row.get("uptime_s"), 1),
                self._fmt(row.get("load_1")),
                self._fmt(row.get("load_5")),
                self._fmt(row.get("load_15")),
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self.summary.setText(
            f"顯示最近 {len(rows)} 筆 native host samples；CPU 第一筆 delta baseline 會顯示 '-'."
        )
