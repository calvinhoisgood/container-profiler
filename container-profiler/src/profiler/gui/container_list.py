"""Container list widget with normalized metadata/tag visibility."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget


class ContainerListWidget(QWidget):
    container_selected = pyqtSignal(str)
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        header_layout = QHBoxLayout()
        title_label = QLabel("容器列表")
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        refresh_btn = QPushButton("刷新")
        refresh_btn.setFixedWidth(60)
        refresh_btn.clicked.connect(self.refresh_requested.emit)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(refresh_btn)
        layout.addLayout(header_layout)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget)

        legend_layout = QHBoxLayout()
        running_dot = QLabel("●")
        running_dot.setStyleSheet("color: #4CAF50;")
        legend_layout.addWidget(running_dot)
        legend_layout.addWidget(QLabel("運行中"))
        stopped_dot = QLabel("●")
        stopped_dot.setStyleSheet("color: #9E9E9E;")
        legend_layout.addWidget(stopped_dot)
        legend_layout.addWidget(QLabel("已停止"))
        legend_layout.addStretch()
        layout.addLayout(legend_layout)

    def update_list(self, containers):
        current_id = None
        if self.list_widget.currentItem():
            current_id = self.list_widget.currentItem().data(Qt.ItemDataRole.UserRole)
        self.list_widget.clear()

        for container in containers:
            identity = []
            if container.service:
                identity.append(f"service:{container.service}")
            if container.env:
                identity.append(f"env:{container.env}")
            if container.version:
                identity.append(f"version:{container.version}")
            extra_tags = [tag for tag in container.tags if tag not in identity][:4]

            lines = [container.name, f"ID: {container.id[:12]} | Image: {container.image}"]
            if identity:
                lines.append(" | ".join(identity))
            if extra_tags:
                lines.append("tags: " + ", ".join(extra_tags))

            item = QListWidgetItem("\n".join(lines))
            item.setData(Qt.ItemDataRole.UserRole, container.id)
            item.setToolTip("\n".join(container.tags) if container.tags else "No extracted tags")
            item.setForeground(QBrush(QColor("#FFFFFF" if container.status == "running" else "#9E9E9E")))
            self.list_widget.addItem(item)
            if current_id and container.id == current_id:
                self.list_widget.setCurrentItem(item)

    def _on_selection_changed(self, current, previous):
        if current:
            self.container_selected.emit(current.data(Qt.ItemDataRole.UserRole))
