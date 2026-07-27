"""
實時曲線控件
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QComboBox, QHBoxLayout, QLabel, QCheckBox
from PyQt6.QtGui import QFont
import pyqtgraph as pg
from collections import deque
import time

class ChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.history_size = 3000  # 緩衝區增至 3000 點
        self.timestamps = deque(maxlen=self.history_size)
        self.data_series = {
            "cpu": deque(maxlen=self.history_size),
            "memory": deque(maxlen=self.history_size),
            "power": deque(maxlen=self.history_size),
            "gpu": deque(maxlen=self.history_size)
        }
        
        self._setup_ui()
        self._init_chart()
        
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # 頂部工具欄
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("實時監控曲線"))
        
        # 顯示模式選擇
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["CPU & Memory", "Power Consumption", "GPU Metrics"])
        self.mode_combo.currentIndexChanged.connect(self._update_chart_mode)
        self.mode_combo.setFixedWidth(150)
        toolbar.addWidget(self.mode_combo)
        
        # 自動縮放開關
        self.auto_scale_cb = QCheckBox("自動縮放 (Auto Scale)")
        self.auto_scale_cb.setStyleSheet("color: #DDDDDD;")
        self.auto_scale_cb.stateChanged.connect(self._toggle_auto_scale)
        toolbar.addWidget(self.auto_scale_cb)
        
        toolbar.addStretch()
        layout.addLayout(toolbar)
        
        # 圖表區域
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#000000') # 純黑背景
        
        # 設置坐標軸樣式
        styles = {'color': '#DDDDDD', 'font-size': '12px'}
        self.plot_widget.setLabel('left', 'Value', **styles)
        self.plot_widget.setLabel('bottom', 'Time (s)', **styles)
        
        # 設置坐標軸刻度顏色
        font = QFont()
        font.setPixelSize(12)
        self.plot_widget.getAxis('bottom').setTickFont(font)
        self.plot_widget.getAxis('left').setTickFont(font)
        self.plot_widget.getAxis('bottom').setPen('#DDDDDD')
        self.plot_widget.getAxis('left').setPen('#DDDDDD')
        self.plot_widget.getAxis('bottom').setTextPen('#DDDDDD')
        self.plot_widget.getAxis('left').setTextPen('#DDDDDD')

        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot_widget)
        
        self.curves = {}
        self.legend = self.plot_widget.addLegend()

    def _init_chart(self):
        # 初始化曲線對象
        pen_cpu = pg.mkPen(color='#2196F3', width=2)
        pen_mem = pg.mkPen(color='#4CAF50', width=2)
        pen_power = pg.mkPen(color='#FFC107', width=2)
        pen_gpu = pg.mkPen(color='#9C27B0', width=2)
        
        self.curves['cpu'] = self.plot_widget.plot(name='CPU %', pen=pen_cpu)
        self.curves['memory'] = self.plot_widget.plot(name='Memory (MB)', pen=pen_mem)
        self.curves['power'] = self.plot_widget.plot(name='Power (W)', pen=pen_power)
        self.curves['gpu'] = self.plot_widget.plot(name='GPU %', pen=pen_gpu)
        
        self._update_chart_mode(0)

    def _toggle_auto_scale(self, state):
        """切換自動縮放"""
        enable = (state == 2) # 2 is Checked
        self.plot_widget.enableAutoRange(axis='y', enable=enable)
        if not enable:
            # 恢復默認視圖
            self._update_chart_mode(self.mode_combo.currentIndex())

    def _update_chart_mode(self, index):
        """切換顯示模式"""
        self.plot_widget.clear()
        self.legend.scene().removeItem(self.legend)
        self.legend = self.plot_widget.addLegend()
        
        # 根據是否勾選自動縮放來決定是否強制設置範圍
        auto_scale = self.auto_scale_cb.isChecked()
        self.plot_widget.enableAutoRange(axis='y', enable=auto_scale)
        
        if index == 0:  # CPU & Memory
            self.plot_widget.addItem(self.curves['cpu'])
            self.plot_widget.addItem(self.curves['memory'])
            self.plot_widget.setLabel('left', 'Usage')
            if not auto_scale:
                self.plot_widget.setYRange(0, 100) # 默認百分比視圖
                
        elif index == 1:  # Power
            self.plot_widget.addItem(self.curves['power'])
            self.plot_widget.setLabel('left', 'Power', units='W')
            if not auto_scale:
                self.plot_widget.enableAutoRange(axis='y', enable=True)
                self.plot_widget.setAutoVisible(y=True) # 確保 0 可見
                
        elif index == 2:  # GPU
            self.plot_widget.addItem(self.curves['gpu'])
            self.plot_widget.setLabel('left', 'GPU Usage')
            if not auto_scale:
                self.plot_widget.setYRange(0, 100)

    def update_data(self, stats, power_stats):
        """更新數據點"""
        now = time.time()
        self.timestamps.append(now)
        
        # 處理數據（None 轉為 0）
        cpu_val = stats.cpu_percent if stats else 0
        mem_val = stats.memory_mb if stats else 0
        power_val = (power_stats.cpu_power_w or 0) + (power_stats.gpu_power_w or 0) if power_stats else 0
        gpu_val = power_stats.gpu_util_percent if (power_stats and power_stats.gpu_util_percent) else 0
        
        self.data_series['cpu'].append(cpu_val)
        self.data_series['memory'].append(mem_val)
        self.data_series['power'].append(power_val)
        self.data_series['gpu'].append(gpu_val)
        
        # 轉為相對時間軸（最近300秒）
        x_axis = [t - now for t in self.timestamps]
        
        # 更新顯示
        if self.mode_combo.currentIndex() == 0:
            self.curves['cpu'].setData(x_axis, list(self.data_series['cpu']))
            self.curves['memory'].setData(x_axis, list(self.data_series['memory']))
        elif self.mode_combo.currentIndex() == 1:
            self.curves['power'].setData(x_axis, list(self.data_series['power']))
        elif self.mode_combo.currentIndex() == 2:
            self.curves['gpu'].setData(x_axis, list(self.data_series['gpu']))
