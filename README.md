# Container Profiler

面向深度學習負載的 Windows 桌面端 Docker 容器運行時監控工具。它將容器資源、CPU/GPU 功耗與實時曲線集中到一個 PyQt6 圖形界面中，並可將採樣結果導出為 CSV，便於性能分析與畢業論文實驗。

## 功能特性

- 瀏覽本機全部 Docker 容器及其運行狀態
- 實時監控容器 CPU、內存與網絡 I/O
- 通過 HWiNFO 共享內存讀取 CPU Package Power
- 通過 NVIDIA NVML 讀取 GPU 功耗、利用率、顯存與溫度
- 使用 PyQtGraph 展示可切換、可縮放的實時曲線
- 自定義採樣間隔，開始或停止監控任務
- 將採樣結果導出為 UTF-8 CSV 文件
- 使用 PyInstaller 打包為獨立 Windows 可執行文件

## 界面與數據流

```text
Docker Engine ── Docker SDK ─┐
                             ├─> 採樣工作線程 ─> 指標卡片 / 實時曲線 ─> CSV
HWiNFO ── Windows 共享內存 ──┤
NVIDIA GPU ── NVML ──────────┘
```

核心模塊分工：

| 模塊 | 職責 |
| --- | --- |
| `core/docker_monitor.py` | 容器發現及 CPU、內存、網絡數據採集 |
| `core/power_monitor.py` | HWiNFO CPU 功耗與 NVML GPU 指標採集 |
| `core/data_manager.py` | 採樣數據緩存及 CSV 導出 |
| `gui/` | 主窗口、容器列表、指標面板與實時圖表 |
| `utils/paths.py` | 源碼和 PyInstaller 打包環境下的路徑解析 |

## 運行環境

- Windows 10/11
- Python 3.11 或更高版本
- Docker Desktop（容器監控必需）
- HWiNFO64，並啟用 **Sensors-only > Shared Memory Support**（CPU 功耗可選）
- NVIDIA 驅動及受 NVML 支持的顯卡（GPU 指標可選）

沒有 HWiNFO 或 NVIDIA GPU 時，對應功耗指標不可用，但容器基礎監控仍可使用。

## 快速開始

```powershell
git clone https://github.com/calvinhoisgood/container-profiler.git
cd container-profiler\container-profiler

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

啟動前請確認 Docker Desktop 已運行。若需 CPU 功耗數據，請同時以 Sensors-only 模式啟動 HWiNFO64 並開啟共享內存支持。

## 打包 Windows 程序

在 `container-profiler` 目錄中執行：

```powershell
pyinstaller --clean build.spec
```

生成的可執行文件位於 `dist/ContainerProfiler_v4.exe`。構建產物不提交到 Git；正式二進制版本應通過 GitHub Releases 發佈。

## 導出字段

CSV 目前包含以下字段：

```text
timestamp, container_id, cpu_percent, memory_mb,
network_rx, network_tx, cpu_power_w, gpu_power_w,
gpu_util, gpu_memory_mb
```

## 項目結構

```text
JNUFINAL/
├── container-profiler/
│   ├── main.py
│   ├── requirements.txt
│   ├── build.spec
│   ├── resources/
│   ├── src/profiler/
│   │   ├── core/
│   │   ├── gui/
│   │   └── utils/
│   └── tests/
├── 文檔/
│   ├── AI_WORKFLOW.md
│   ├── DEVELOPMENT_GUIDE.md
│   └── LOG.md
└── 參考代碼/
```

## 驗證

不依賴硬件的語法檢查：

```powershell
python -m compileall -q container-profiler
```

Docker 集成冒煙測試：

```powershell
cd container-profiler
python tests/test_docker_core.py
```

該測試需要已運行的 Docker Desktop 和至少一個可供採樣的容器。

## 已知限制

- 目前以 Windows 為主要運行平台，HWiNFO 功耗採集依賴 Windows 共享內存。
- GPU 指標僅支持 NVIDIA NVML。
- 現有測試為本機 Docker 集成冒煙測試，尚未建立完整的自動化單元測試。
- HWiNFO 共享內存結構可能隨版本變化，升級 HWiNFO 後應重新驗證功耗讀數。

## 文檔

- [完整開發指南](文檔/DEVELOPMENT_GUIDE.md)
- [AI 協作流程](文檔/AI_WORKFLOW.md)
- [開發日誌](文檔/LOG.md)

## 項目背景

本項目服務於「面向深度學習負載的容器運行時畫像與工具集構建」畢業設計，目標是以低門檻桌面工具採集容器及硬件運行數據，為不同模型、推理框架與部署配置的性能和能耗比較提供可復現的數據基礎。

