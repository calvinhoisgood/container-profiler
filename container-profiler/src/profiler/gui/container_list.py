"""
容器列表控件
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, 
    QLabel, QPushButton, QHBoxLayout
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QIcon, QColor, QBrush

class ContainerListWidget(QWidget):
    # 信號：當選擇容器時觸發，傳遞容器ID
    container_selected = pyqtSignal(str)
    # 信號：刷新列表
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # 標題欄
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
        
        # 列表控件
        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget)
        
        # 狀態說明
        legend_layout = QHBoxLayout()
        running_dot = QLabel("●")
        running_dot.setStyleSheet("color: #4CAF50;") # Green
        legend_layout.addWidget(running_dot)
        legend_layout.addWidget(QLabel("運行中"))
        
        stopped_dot = QLabel("●")
        stopped_dot.setStyleSheet("color: #9E9E9E;") # Grey
        legend_layout.addWidget(stopped_dot)
        legend_layout.addWidget(QLabel("已停止"))
        
        legend_layout.addStretch()
        layout.addLayout(legend_layout)

    def update_list(self, containers):
        """
        更新容器列表
        :param containers: List[ContainerInfo]
        """
        current_id = None
        if self.list_widget.currentItem():
            current_id = self.list_widget.currentItem().data(Qt.ItemDataRole.UserRole)
            
        self.list_widget.clear()
        
        for container in containers:
            item = QListWidgetItem()
            # 設置顯示文本
            display_text = f"{container.name}\nID: {container.id[:12]} | Image: {container.image}"
            item.setText(display_text)
            
            # 存儲 ID
            item.setData(Qt.ItemDataRole.UserRole, container.id)
            
            # 設置圖標/顏色狀態
            if container.status == "running":
                item.setIcon(QIcon()) # TODO: 可以加個綠色圖標
                item.setForeground(QBrush(QColor("#FFFFFF")))
            else:
                item.setForeground(QBrush(QColor("#9E9E9E")))
                
            self.list_widget.addItem(item)
            
            # 恢復選擇
            if current_id and container.id == current_id:
                self.list_widget.setCurrentItem(item)

    def _on_selection_changed(self, current, previous):
        if current:
            container_id = current.data(Qt.ItemDataRole.UserRole)
            self.container_selected.emit(container_id)
