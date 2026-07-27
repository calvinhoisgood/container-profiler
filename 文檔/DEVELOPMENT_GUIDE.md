# Container Profiler 完整開發指南

> **論文題目**: 面向深度學習負載的容器運行時畫像與工具集構建  
> **方案選擇**: 方案 C - 桌面 GUI 應用  
> **最後更新**: 2026-01-27

---

## 一、項目概述

### 1.1 目標

創建一個**獨立可執行的桌面應用程序** (`.exe`)，用於：
- 監控任意 Docker 容器的運行時性能
- 採集 CPU/GPU 功耗、利用率等指標
- 提供實時可視化曲線
- 導出數據用於論文分析

### 1.2 最終產物

```
ContainerProfiler.exe (約 120-150 MB)
├── 雙擊即可運行
├── 無需安裝 Python
├── 無需額外依賴
└── 深色主題現代界面
```

### 1.3 功能列表

| 功能 | 描述 | 優先級 |
|:-----|:-----|:------:|
| 容器列表 | 顯示所有 Docker 容器 | P0 |
| 資源監控 | CPU%, Memory, Network I/O | P0 |
| 功耗監控 | CPU Package Power (HWiNFO) | P0 |
| GPU 監控 | GPU Power, 顯存, 利用率 (NVML) | P1 |
| 實時曲線 | 滾動時間軸圖表 | P0 |
| 數據導出 | CSV 文件導出 | P0 |
| 狀態機 | 自動檢測容器狀態 | P2 |

---

## 二、技術架構

### 2.1 技術棧

| 類別 | 技術選擇 | 版本 | 用途 |
|:-----|:---------|:-----|:-----|
| 語言 | Python | 3.11+ | 主開發語言 |
| GUI 框架 | PyQt6 | 6.6+ | 桌面界面 |
| 圖表庫 | PyQtGraph | 0.13+ | 實時曲線 |
| Docker | docker-py | 7.0+ | 容器 API |
| GPU | nvidia-ml-py (`pynvml`) | 12.0+ | NVIDIA 監控 |
| 打包 | PyInstaller | 6.0+ | exe 生成 |

### 2.2 系統架構圖

```
┌─────────────────────────────────────────────────────────────────┐
│                     ContainerProfiler.exe                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │                      GUI 層 (PyQt6)                      │   │
│   │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │   │
│   │  │ ContainerList│  │ MetricsPanel │  │  ChartWidget   │  │   │
│   │  │  容器列表    │  │  指標卡片    │  │  實時曲線      │  │   │
│   │  └─────────────┘  └──────────────┘  └────────────────┘  │   │
│   └──────────────────────────┬──────────────────────────────┘   │
│                              │                                   │
│   ┌──────────────────────────▼──────────────────────────────┐   │
│   │                    核心層 (Core)                         │   │
│   │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │   │
│   │  │DockerMonitor│  │ PowerMonitor │  │  DataManager   │  │   │
│   │  │ 容器監控    │  │  功耗監控    │  │  數據管理      │  │   │
│   │  └──────┬──────┘  └──────┬───────┘  └────────────────┘  │   │
│   └─────────┼────────────────┼──────────────────────────────┘   │
│             │                │                                   │
└─────────────┼────────────────┼───────────────────────────────────┘
              ▼                ▼
      ┌───────────────┐  ┌──────────────┬──────────────┐
      │  Docker API   │  │   HWiNFO64   │  NVIDIA NVML │
      │  (容器指標)   │  │  (CPU功耗)   │  (GPU指標)   │
      └───────────────┘  └──────────────┴──────────────┘
```

---

## 三、項目結構

```
D:\JNUFINAL\container-profiler\
│
├── 📄 main.py                      ← 主入口 (打包此文件)
├── 📄 requirements.txt             ← Python 依賴
├── 📄 build.spec                   ← PyInstaller 配置
├── 📄 README.md                    ← 項目說明
│
├── 📂 src\
│   └── 📂 profiler\
│       ├── __init__.py
│       ├── __main__.py             ← python -m profiler 支持
│       │
│       ├── 📂 gui\                 ← GUI 模組
│       │   ├── __init__.py
│       │   ├── main_window.py      ← 主窗口
│       │   ├── container_list.py   ← 容器列表控件
│       │   ├── metrics_panel.py    ← 指標數值面板
│       │   ├── chart_widget.py     ← 實時曲線控件
│       │   └── settings_dialog.py  ← 設置對話框
│       │
│       ├── 📂 core\                ← 核心邏輯
│       │   ├── __init__.py
│       │   ├── docker_monitor.py   ← Docker 監控
│       │   ├── power_monitor.py    ← 功耗監控 (HWiNFO + NVML)
│       │   ├── data_manager.py     ← 數據收集與緩衝
│       │   └── event_detector.py   ← 狀態機事件檢測
│       │
│       └── 📂 utils\               ← 工具模組
│           ├── __init__.py
│           ├── paths.py            ← 路徑處理 (打包兼容)
│           ├── config.py           ← 配置管理
│           └── logger.py           ← 日誌設置
│
├── 📂 resources\                   ← 靜態資源 (打包進 exe)
│   ├── icon.ico                    ← 程序圖標
│   ├── icon.png                    ← 高清圖標
│   └── 📂 styles\
│       └── dark_theme.qss          ← 深色主題樣式表
│
├── 📂 tests\                       ← 測試腳本
│   ├── test_docker.py
│   ├── test_power.py
│   └── test_gui.py
│
├── 📂 docker\                      ← 示範容器 (不打包)
│   └── 📂 demo\
│       ├── server.py
│       └── Dockerfile
│
└── 📂 dist\                        ← 打包輸出
    └── ContainerProfiler.exe
```

---

## 四、環境準備

### 4.1 前置要求

| 軟件 | 版本 | 必須 | 用途 |
|:-----|:-----|:----:|:-----|
| Windows | 10/11 | ✅ | 操作系統 |
| Python | 3.11+ | ✅ | 開發環境 |
| Docker Desktop | 24.0+ | ✅ | 容器運行 |
| HWiNFO64 | 最新版 | ✅ | CPU 功耗 |
| NVIDIA 驅動 | 535+ | ❓ | GPU 監控 |

### 4.2 HWiNFO 配置

1. 下載安裝 [HWiNFO64](https://www.hwinfo.com/)
2. 運行 HWiNFO **Sensors-only 模式**
3. 設置 → Sensors Settings → **勾選 Shared Memory Support**
4. 保持 HWiNFO 後台運行

### 4.3 創建開發環境

```powershell
# 1. 創建項目目錄
cd D:\JNUFINAL
mkdir container-profiler
cd container-profiler

# 2. 創建虛擬環境
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. 安裝依賴
pip install PyQt6 pyqtgraph docker nvidia-ml-py construct pyinstaller
pip freeze > requirements.txt
```

---

## 五、開發階段

### 階段 1：項目骨架 (1-2 小時)

**目標**：創建能打包的空窗口

**文件**：`main.py`
```python
"""
Container Profiler - 容器運行時畫像工具
主入口文件
"""
import sys
from pathlib import Path

# 添加 src 到路徑
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from profiler.gui.main_window import MainWindow
from profiler.utils.paths import get_resource_path

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Container Profiler")
    
    # 設置圖標
    icon_path = get_resource_path("resources/icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
```

**文件**：`src/profiler/utils/paths.py`
```python
"""
路徑處理工具 - 兼容開發環境和打包後環境
"""
import sys
from pathlib import Path

def get_base_path() -> Path:
    """獲取項目根目錄"""
    if getattr(sys, 'frozen', False):
        # 打包後: exe 所在目錄
        return Path(sys.executable).parent
    else:
        # 開發時: main.py 所在目錄
        return Path(__file__).parent.parent.parent.parent

def get_resource_path(relative_path: str) -> Path:
    """獲取資源文件路徑"""
    return get_base_path() / relative_path

def get_config_dir() -> Path:
    """獲取配置文件目錄 (用戶目錄)"""
    config_dir = Path.home() / ".container-profiler"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_output_dir() -> Path:
    """獲取輸出目錄"""
    output_dir = get_config_dir() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
```

**文件**：`src/profiler/gui/main_window.py`
```python
"""
主窗口
"""
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QStatusBar, QPushButton
)
from PyQt6.QtCore import Qt

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Container Profiler")
        self.setGeometry(100, 100, 1400, 900)
        self.setMinimumSize(1000, 600)
        
        self._setup_ui()
        self._setup_statusbar()
    
    def _setup_ui(self):
        """設置界面佈局"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主佈局: 左右分欄
        main_layout = QHBoxLayout(central_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 左側: 容器列表 (佔 25%)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QPushButton("容器列表 (待實現)"))
        
        # 右側: 監控面板 (佔 75%)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QPushButton("監控區域 (待實現)"))
        
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 1100])
        
        main_layout.addWidget(splitter)
    
    def _setup_statusbar(self):
        """設置狀態欄"""
        self.statusBar().showMessage("就緒")
```

**驗收**：
```powershell
python main.py  # 應彈出空窗口
```

---

### 階段 2：UI 完整佈局 (2-3 小時)

**目標**：完成所有 UI 控件佈局

**主界面結構**：
```
┌─────────────────────────────────────────────────────────────────┐
│  Container Profiler                              [—] [□] [×]    │
├─────────────────────────────────────────────────────────────────┤
│ ┌─────────────┐ ┌─────────────────────────────────────────────┐ │
│ │ 容器列表     │ │  ┌─────────────────────────────────────┐   │ │
│ │             │ │  │          實時曲線區域              │   │ │
│ │ ● running_1 │ │  │   CPU / Memory / Power 曲線        │   │ │
│ │ ○ stopped_1 │ │  │                                    │   │ │
│ │ ● running_2 │ │  └─────────────────────────────────────┘   │ │
│ │             │ │  ┌───────┬───────┬───────┬───────────┐    │ │
│ │             │ │  │ CPU % │Memory │CPU(W) │ GPU (W)   │    │ │
│ │             │ │  │ 45.2  │ 512MB │ 42.3  │  125.6    │    │ │
│ │             │ │  └───────┴───────┴───────┴───────────┘    │ │
│ │ [刷新列表]  │ │  [開始監控] [停止] [導出CSV] [⚙設置]      │ │
│ └─────────────┘ └─────────────────────────────────────────────┘ │
│ 狀態: Docker 已連接 | HWiNFO 已連接 | 監控中: container_1        │
└─────────────────────────────────────────────────────────────────┘
```

---

### 階段 3：Docker 監控核心 (3-4 小時)

**文件**：`src/profiler/core/docker_monitor.py`
```python
"""
Docker 容器監控模組
"""
import docker
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ContainerInfo:
    """容器信息"""
    id: str
    name: str
    status: str  # running, exited, paused
    image: str
    created: datetime

@dataclass
class ContainerStats:
    """容器資源統計"""
    timestamp: datetime
    cpu_percent: float
    memory_mb: float
    memory_limit_mb: float
    network_rx_bytes: int
    network_tx_bytes: int

class DockerMonitor:
    """Docker 監控器"""
    
    def __init__(self):
        self.client: Optional[docker.DockerClient] = None
        self._connect()
    
    def _connect(self):
        """連接 Docker"""
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            self.client = None
            raise ConnectionError(f"無法連接 Docker: {e}")
    
    def is_connected(self) -> bool:
        """檢查連接狀態"""
        if not self.client:
            return False
        try:
            self.client.ping()
            return True
        except:
            return False
    
    def list_containers(self, all: bool = True) -> List[ContainerInfo]:
        """獲取容器列表"""
        if not self.client:
            return []
        
        containers = self.client.containers.list(all=all)
        return [
            ContainerInfo(
                id=c.short_id,
                name=c.name,
                status=c.status,
                image=c.image.tags[0] if c.image.tags else "unknown",
                created=c.attrs.get("Created", "")
            )
            for c in containers
        ]
    
    def get_stats(self, container_name: str) -> Optional[ContainerStats]:
        """獲取容器即時統計"""
        if not self.client:
            return None
        
        try:
            container = self.client.containers.get(container_name)
            stats = container.stats(stream=False)
            
            # 計算 CPU 使用率
            cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                       stats["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = stats["cpu_stats"]["system_cpu_usage"] - \
                          stats["precpu_stats"]["system_cpu_usage"]
            cpu_count = stats["cpu_stats"]["online_cpus"]
            cpu_percent = (cpu_delta / system_delta) * cpu_count * 100 if system_delta > 0 else 0
            
            # 內存
            memory_usage = stats["memory_stats"].get("usage", 0)
            memory_limit = stats["memory_stats"].get("limit", 1)
            
            # 網絡
            networks = stats.get("networks", {})
            rx_bytes = sum(v.get("rx_bytes", 0) for v in networks.values())
            tx_bytes = sum(v.get("tx_bytes", 0) for v in networks.values())
            
            return ContainerStats(
                timestamp=datetime.now(),
                cpu_percent=round(cpu_percent, 2),
                memory_mb=round(memory_usage / 1024 / 1024, 2),
                memory_limit_mb=round(memory_limit / 1024 / 1024, 2),
                network_rx_bytes=rx_bytes,
                network_tx_bytes=tx_bytes
            )
        except Exception as e:
            print(f"獲取統計失敗: {e}")
            return None
```

---

### 階段 4：功耗監控 (3-4 小時)

**文件**：`src/profiler/core/power_monitor.py`
```python
"""
功耗監控模組 - HWiNFO + NVIDIA NVML
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional
import struct

@dataclass
class PowerStats:
    """功耗統計"""
    cpu_power_w: Optional[float] = None  # CPU 封裝功耗
    gpu_power_w: Optional[float] = None  # GPU 功耗
    gpu_util_percent: Optional[float] = None  # GPU 利用率
    gpu_memory_mb: Optional[float] = None  # GPU 顯存使用
    gpu_temp_c: Optional[float] = None  # GPU 溫度

class HWiNFOReader:
    """HWiNFO 共享內存讀取器"""
    
    HWINFO_SENSORS_SM_NAME = "Global\\HWiNFO_SENS_SM2"
    
    def __init__(self):
        self.hMapFile = None
        self.pBuf = None
        self._connect()
    
    def _connect(self):
        """連接 HWiNFO 共享內存"""
        kernel32 = ctypes.windll.kernel32
        
        self.hMapFile = kernel32.OpenFileMappingW(
            0x0004,  # FILE_MAP_READ
            False,
            self.HWINFO_SENSORS_SM_NAME
        )
        
        if not self.hMapFile:
            raise ConnectionError("無法連接 HWiNFO，請確保已啟用 Shared Memory")
        
        self.pBuf = kernel32.MapViewOfFile(
            self.hMapFile,
            0x0004,  # FILE_MAP_READ
            0, 0, 0
        )
    
    def is_connected(self) -> bool:
        return self.pBuf is not None
    
    def get_cpu_power(self) -> Optional[float]:
        """讀取 CPU Package Power"""
        if not self.pBuf:
            return None
        
        try:
            # 讀取 HWiNFO 結構 (簡化版)
            # 實際需要解析完整的 HWiNFO 數據結構
            # 這裡返回模擬值，實際實現需要遍歷傳感器找到 CPU Package Power
            return None  # 待實現完整解析
        except Exception as e:
            print(f"讀取 CPU 功耗失敗: {e}")
            return None
    
    def close(self):
        if self.pBuf:
            ctypes.windll.kernel32.UnmapViewOfFile(self.pBuf)
        if self.hMapFile:
            ctypes.windll.kernel32.CloseHandle(self.hMapFile)

class NVMLReader:
    """NVIDIA GPU 監控器"""
    
    def __init__(self):
        self.initialized = False
        self._init()
    
    def _init(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            self.pynvml = pynvml
            self.device_count = pynvml.nvmlDeviceGetCount()
            self.initialized = True
        except Exception as e:
            print(f"NVML 初始化失敗 (可能沒有 NVIDIA 顯卡): {e}")
            self.initialized = False
    
    def is_available(self) -> bool:
        return self.initialized and self.device_count > 0
    
    def get_gpu_stats(self, device_index: int = 0) -> Optional[dict]:
        """獲取 GPU 統計"""
        if not self.initialized:
            return None
        
        try:
            handle = self.pynvml.nvmlDeviceGetHandleByIndex(device_index)
            
            power = self.pynvml.nvmlDeviceGetPowerUsage(handle) / 1000  # mW -> W
            util = self.pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory = self.pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = self.pynvml.nvmlDeviceGetTemperature(handle, 0)
            
            return {
                "power_w": round(power, 2),
                "gpu_util": util.gpu,
                "memory_used_mb": round(memory.used / 1024 / 1024, 2),
                "memory_total_mb": round(memory.total / 1024 / 1024, 2),
                "temperature_c": temp
            }
        except Exception as e:
            print(f"讀取 GPU 統計失敗: {e}")
            return None
    
    def close(self):
        if self.initialized:
            try:
                self.pynvml.nvmlShutdown()
            except:
                pass

class PowerMonitor:
    """統一功耗監控接口"""
    
    def __init__(self):
        self.hwinfo: Optional[HWiNFOReader] = None
        self.nvml: Optional[NVMLReader] = None
        
        # 嘗試連接 HWiNFO
        try:
            self.hwinfo = HWiNFOReader()
        except Exception as e:
            print(f"HWiNFO 不可用: {e}")
        
        # 嘗試初始化 NVML
        self.nvml = NVMLReader()
    
    def get_power_stats(self) -> PowerStats:
        """獲取功耗統計"""
        stats = PowerStats()
        
        # CPU 功耗
        if self.hwinfo and self.hwinfo.is_connected():
            stats.cpu_power_w = self.hwinfo.get_cpu_power()
        
        # GPU 統計
        if self.nvml and self.nvml.is_available():
            gpu_stats = self.nvml.get_gpu_stats()
            if gpu_stats:
                stats.gpu_power_w = gpu_stats["power_w"]
                stats.gpu_util_percent = gpu_stats["gpu_util"]
                stats.gpu_memory_mb = gpu_stats["memory_used_mb"]
                stats.gpu_temp_c = gpu_stats["temperature_c"]
        
        return stats
    
    def close(self):
        if self.hwinfo:
            self.hwinfo.close()
        if self.nvml:
            self.nvml.close()
```

---

### 階段 5：實時曲線與數據導出 (3-4 小時)

**目標**：
- PyQtGraph 實時滾動曲線
- 數據緩衝 (保留最近 5 分鐘)
- CSV 導出功能

---

### 階段 6：打包與優化 (2-3 小時)

**文件**：`build.spec`
```python
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('resources', 'resources'),
    ],
    hiddenimports=[
        'PyQt6.sip',
        'pyqtgraph',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'tkinter', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ContainerProfiler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 無控制台窗口
    icon='resources/icon.ico',
)
```

**打包命令**：
```powershell
pyinstaller build.spec
# 輸出: dist/ContainerProfiler.exe
```

---

## 六、採集指標說明

### 6.1 容器指標 (來源: Docker API)

| 指標 | 單位 | 說明 |
|:-----|:-----|:-----|
| CPU 使用率 | % | 容器 CPU 佔用百分比 |
| 內存使用量 | MB | 容器內存佔用 |
| 內存限制 | MB | 容器內存上限 |
| 網絡接收 | Bytes | 累計接收字節 |
| 網絡發送 | Bytes | 累計發送字節 |

### 6.2 功耗指標 (來源: HWiNFO)

| 指標 | 單位 | 說明 |
|:-----|:-----|:-----|
| CPU Package Power | W | CPU 封裝總功耗 |
| CPU Cores Power | W | CPU 核心功耗 |

### 6.3 GPU 指標 (來源: NVML)

| 指標 | 單位 | 說明 |
|:-----|:-----|:-----|
| GPU Power | W | GPU 功耗 |
| GPU 利用率 | % | GPU 計算單元使用率 |
| 顯存使用量 | MB | 已使用顯存 |
| GPU 溫度 | °C | GPU 核心溫度 |

### 6.4 CSV 導出格式

```csv
timestamp,container_name,cpu_percent,memory_mb,cpu_power_w,gpu_power_w,gpu_util_percent
2026-01-27T11:30:00,my_container,45.2,512.0,42.3,125.6,78.0
2026-01-27T11:30:01,my_container,46.8,515.2,43.1,128.2,82.0
...
```

---

## 七、驗證清單

開發完成後，請逐項驗證：

- [ ] **階段 1**: `python main.py` 彈出空窗口
- [ ] **階段 1**: `pyinstaller build.spec` 打包成功
- [ ] **階段 2**: UI 佈局完整顯示
- [ ] **階段 3**: 容器列表正確顯示
- [ ] **階段 3**: 選擇容器後顯示 CPU/Memory
- [ ] **階段 4**: HWiNFO 功耗數據正確讀取
- [ ] **階段 4**: GPU 功耗數據正確讀取 (如有)
- [ ] **階段 5**: 實時曲線滾動正常
- [ ] **階段 5**: CSV 導出文件正確
- [ ] **階段 6**: 最終 exe 雙擊可運行
- [ ] **階段 6**: exe 體積在預期範圍內

---

## 八、常見問題

### Q1: Docker 連接失敗
```powershell
# 確保 Docker Desktop 已啟動
docker info
```

### Q2: HWiNFO 連接失敗
1. 運行 HWiNFO64 (Sensors-only 模式)
2. 設置 → Sensors Settings → 勾選 Shared Memory Support

### Q3: 打包後缺少依賴
```powershell
# 檢查隱藏導入
pyinstaller --hidden-import=xxx main.py
```

### Q4: 圖標不顯示
確保 `resources/icon.ico` 存在且格式正確

---

## 九、參考資料

- [PyQt6 文檔](https://www.riverbankcomputing.com/static/Docs/PyQt6/)
- [PyQtGraph 文檔](https://pyqtgraph.readthedocs.io/)
- [Docker SDK for Python](https://docker-py.readthedocs.io/)
- [PyInstaller 文檔](https://pyinstaller.org/en/stable/)
- [NVIDIA NVML](https://developer.nvidia.com/nvidia-management-library-nvml)

---

> **此文檔為唯一開發參考文檔，涵蓋從零到打包的完整流程。**
