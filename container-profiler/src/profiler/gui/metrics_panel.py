"""
指標數值面板
"""
from PyQt6.QtWidgets import QWidget, QGridLayout, QLabel, QVBoxLayout, QFrame
from PyQt6.QtCore import Qt

class MetricCard(QFrame):
    def __init__(self, title, unit, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setStyleSheet("""
            QFrame {
                background-color: #2D2D2D;
                border-radius: 5px;
                border: 1px solid #3D3D3D;
            }
            QLabel {
                border: none;
                background-color: transparent;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # 標題
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: #AAAAAA; font-size: 12px;")
        layout.addWidget(self.title_label)
        
        # 數值
        self.value_label = QLabel("-")
        self.value_label.setStyleSheet("color: #FFFFFF; font-size: 24px; font-weight: bold;")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.value_label)
        
        # 單位
        self.unit_label = QLabel(unit)
        self.unit_label.setStyleSheet("color: #AAAAAA; font-size: 12px;")
        self.unit_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.unit_label)

    def update_value(self, value):
        self.value_label.setText(str(value))

class MetricsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        
    def _setup_ui(self):
        layout = QGridLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(10)
        
        # CPU
        self.cpu_usage = MetricCard("CPU 使用率", "%")
        layout.addWidget(self.cpu_usage, 0, 0)
        
        # Memory
        self.mem_usage = MetricCard("內存使用", "MB")
        layout.addWidget(self.mem_usage, 0, 1)
        
        # CPU Power
        self.cpu_power = MetricCard("CPU 功耗", "W")
        layout.addWidget(self.cpu_power, 0, 2)
        
        # GPU Power
        self.gpu_power = MetricCard("GPU 功耗", "W")
        layout.addWidget(self.gpu_power, 0, 3)
        
        # GPU Util
        self.gpu_util = MetricCard("GPU 利用率", "%")
        layout.addWidget(self.gpu_util, 1, 0)
        
        # GPU Memory
        self.gpu_mem = MetricCard("顯存使用", "MB")
        layout.addWidget(self.gpu_mem, 1, 1)
        
        # Network RX
        self.net_rx = MetricCard("網絡接收", "KB/s")
        layout.addWidget(self.net_rx, 1, 2)
        
        # Network TX
        self.net_tx = MetricCard("網絡發送", "KB/s")
        layout.addWidget(self.net_tx, 1, 3)

    def update_metrics(self, stats, power_stats):
        """
        更新顯示
        :param stats: ContainerStats
        :param power_stats: PowerStats
        """
        if stats:
            self.cpu_usage.update_value(f"{stats.cpu_percent:.1f}")
            self.mem_usage.update_value(f"{stats.memory_mb:.1f}")
            # 簡單計算網絡速率（這裡簡化處理，實際需要計算差值）
            self.net_rx.update_value("0.0") 
            self.net_tx.update_value("0.0")
            
        if power_stats:
            if power_stats.cpu_power_w is not None:
                self.cpu_power.update_value(f"{power_stats.cpu_power_w:.1f}")
            
            if power_stats.gpu_power_w is not None:
                self.gpu_power.update_value(f"{power_stats.gpu_power_w:.1f}")
                self.gpu_util.update_value(f"{power_stats.gpu_util_percent:.1f}")
                self.gpu_mem.update_value(f"{power_stats.gpu_memory_mb:.0f}")
