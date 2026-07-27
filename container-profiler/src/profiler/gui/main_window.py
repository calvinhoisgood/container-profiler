"""
主窗口 - 僅限真實數據
"""
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QStatusBar, QPushButton, QFrame, QFileDialog, QMessageBox,
    QSpinBox, QLabel
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from datetime import datetime
import time
from .container_list import ContainerListWidget
from .metrics_panel import MetricsPanel
from .chart_widget import ChartWidget
from ..core.docker_monitor import DockerMonitor
from ..core.power_monitor import PowerMonitor
from ..core.data_manager import DataManager
from ..utils.paths import get_output_dir

class WorkerThread(QThread):
    """
    後台採集線程 - 解決主界面卡頓問題
    """
    data_ready = pyqtSignal(object, object)  # (stats, power_stats)
    error_occurred = pyqtSignal(str)

    def __init__(self, docker_monitor, power_monitor, container_id, interval_ms):
        super().__init__()
        self.docker_monitor = docker_monitor
        self.power_monitor = power_monitor
        self.container_id = container_id
        self.interval_ms = interval_ms
        self.is_running = True

    def run(self):
        while self.is_running:
            try:
                start_time = time.time()
                
                # 同步採集數據（耗時操作）
                stats = self.docker_monitor.get_stats(self.container_id)
                power_stats = self.power_monitor.get_power_stats()
                
                if stats:
                    self.data_ready.emit(stats, power_stats)
                else:
                    self.error_occurred.emit("獲取容器數據失敗，容器可能已停止")
                    break
                
                # 計算剩餘等待時間，保持精確頻率
                elapsed = (time.time() - start_time) * 1000
                sleep_time = max(1, int(self.interval_ms - elapsed))
                
                self.msleep(sleep_time)
                
            except Exception as e:
                self.error_occurred.emit(f"監控線程錯誤: {str(e)}")
                break

    def stop(self):
        self.is_running = False
        self.wait()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Container Profiler")
        self.setGeometry(100, 100, 1400, 900)
        self.setMinimumSize(1000, 600)
        
        # 核心組件
        self.docker_monitor = DockerMonitor()
        self.power_monitor = PowerMonitor()
        self.data_manager = DataManager()
        
        # UI 組件
        self.container_list = None
        self.metrics_panel = None
        self.chart_widget = None
        self.interval_spin = None
        
        self.selected_container_id = None
        self.is_monitoring = False
        self.worker_thread = None # 監控線程
        
        self._setup_ui()
        self._setup_statusbar()
        self._setup_style()
        
        # 信號連接
        self.container_list.refresh_requested.connect(self._refresh_container_list)
        self.container_list.container_selected.connect(self._on_container_selected)
        self.start_btn.clicked.connect(self._toggle_monitoring)
        self.export_btn.clicked.connect(self._export_data)
        
        # 初始加載
        self._refresh_container_list()

    def _refresh_container_list(self):
        """刷新容器列表"""
        containers = self.docker_monitor.list_containers()
        self.container_list.update_list(containers)
        if not containers:
            self.statusBar().showMessage("未發現容器，請確保 Docker 已啟動並有運行中的容器")
        
    def _on_container_selected(self, container_id):
        """選擇容器"""
        self.selected_container_id = container_id
        self.statusBar().showMessage(f"已選擇容器: {container_id[:12]}")

    def _toggle_monitoring(self, checked):
        """切換監控狀態"""
        self.is_monitoring = checked
        if self.is_monitoring:
            if not self.selected_container_id:
                QMessageBox.warning(self, "警告", "請先選擇一個容器")
                self.start_btn.setChecked(False)
                self.is_monitoring = False
                return
            
            # 獲取採樣間隔
            interval = self.interval_spin.value()
            
            # 開始記錄
            self.data_manager.start_recording()
            
            # 啟動後台線程
            self.worker_thread = WorkerThread(
                self.docker_monitor, 
                self.power_monitor, 
                self.selected_container_id, 
                interval
            )
            self.worker_thread.data_ready.connect(self._handle_new_data)
            self.worker_thread.error_occurred.connect(self._handle_monitor_error)
            self.worker_thread.start()
            
            self.statusBar().showMessage(f"正在監控: {self.selected_container_id[:12]} (頻率: {interval}ms)")
            self.export_btn.setEnabled(False)
            self.interval_spin.setEnabled(False)
        else:
            # 停止線程
            if self.worker_thread:
                self.worker_thread.stop()
                self.worker_thread = None
                
            self.data_manager.stop_recording()
            self.statusBar().showMessage(f"監控已停止 | 已記錄 {len(self.data_manager.recorded_data)} 條數據")
            self.export_btn.setEnabled(True)
            self.interval_spin.setEnabled(True)

    def _handle_new_data(self, stats, power_stats):
        """處理新數據 (在主線程執行)"""
        # 1. 記錄數據
        self.data_manager.add_record(self.selected_container_id, stats, power_stats)
        
        # 2. 更新 UI
        self.metrics_panel.update_metrics(stats, power_stats)
        self.chart_widget.update_data(stats, power_stats)
        
    def _handle_monitor_error(self, error_msg):
        """處理監控錯誤"""
        self.statusBar().showMessage(error_msg)
        self.start_btn.setChecked(False)
        self._toggle_monitoring(False)
            
    def _export_data(self):
        """導出數據"""
        if not self.data_manager.recorded_data:
            QMessageBox.information(self, "提示", "暫無數據可導出")
            return
            
        default_name = f"profiler_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        default_path = get_output_dir() / default_name
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "導出 CSV", str(default_path), "CSV Files (*.csv)"
        )
        
        if file_path:
            if self.data_manager.export_csv(file_path):
                QMessageBox.information(self, "成功", f"數據已導出至:\n{file_path}")
            else:
                QMessageBox.critical(self, "錯誤", "導出失敗")
    
    def _setup_style(self):
        # 設置深色主題背景
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1E1E1E;
            }
            QWidget {
                color: #FFFFFF;
                font-family: 'Segoe UI', Arial;
            }
            QSplitter::handle {
                background-color: #3D3D3D;
            }
            QListWidget {
                background-color: #252526;
                border: 1px solid #3D3D3D;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #094771;
                color: #FFFFFF;
            }
            QComboBox {
                background-color: #2D2D2D;
                color: #FFFFFF;
                border: 1px solid #3D3D3D;
                border-radius: 4px;
                padding: 4px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #AAAAAA;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #2D2D2D;
                color: #FFFFFF;
                selection-background-color: #094771;
                outline: none;
            }
            QLabel {
                color: #FFFFFF;
            }
            QSpinBox {
                background-color: #2D2D2D;
                color: #FFFFFF;
                border: 1px solid #3D3D3D;
                border-radius: 4px;
                padding: 4px;
            }
        """)

    def _setup_ui(self):
        """設置界面佈局"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        self.container_list = ContainerListWidget()
        splitter.addWidget(self.container_list)
        
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 0, 0, 0)
        
        control_layout = QHBoxLayout()
        
        # 監控控制按鈕
        self.start_btn = QPushButton("開始監控")
        self.start_btn.setCheckable(True)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #007ACC;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:checked {
                background-color: #DA3633;
                text: "停止監控";
            }
            QPushButton:hover {
                background-color: #1E8AD6;
            }
        """)
        
        self.export_btn = QPushButton("導出數據")
        self.export_btn.setStyleSheet("""
            QPushButton {
                background-color: #3D3D3D;
                border: 1px solid #555555;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #4D4D4D;
            }
        """)
        
        # 頻率設置
        freq_layout = QHBoxLayout()
        freq_label = QLabel("頻率:")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 1000) # 20ms 到 1000ms
        self.interval_spin.setSingleStep(10)
        self.interval_spin.setValue(1000) # 默認 1秒
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setFixedWidth(100)
        
        freq_layout.addWidget(freq_label)
        freq_layout.addWidget(self.interval_spin)
        
        control_layout.addWidget(self.start_btn)
        control_layout.addSpacing(10)
        control_layout.addLayout(freq_layout)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.export_btn)
        control_layout.addStretch()
        
        right_layout.addLayout(control_layout)
        
        self.chart_widget = ChartWidget()
        right_layout.addWidget(self.chart_widget, stretch=2)
        
        self.metrics_panel = MetricsPanel()
        right_layout.addWidget(self.metrics_panel, stretch=1)
        
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 1100])
        
        main_layout.addWidget(splitter)
    
    def _setup_statusbar(self):
        """設置狀態欄"""
        docker_conn = "已連接" if self.docker_monitor.is_connected() else "未連接"
        nvml_conn = "GPU已就緒" if self.power_monitor.nvml.is_available() else "無GPU"
        hwinfo_conn = "HWiNFO已就緒" if self.power_monitor.hwinfo.is_connected() else "HWiNFO未就緒"
        
        self.statusBar().showMessage(f"Docker: {docker_conn} | {nvml_conn} | {hwinfo_conn}")
        self.statusBar().setStyleSheet("color: #FFFFFF; background-color: #007ACC;")