# 開發日誌

## 2026-09-15
### 17:17 - Headless Agent lifecycle、bounded ingestion 與可靠傳輸整合
- **類型**: 架構 / 功能 / 可靠性 / 測試
- **分支**: `gpt56-rebuild-v5`
- **狀態**: ✅ hardware-free GitHub Actions 驗證持續通過
- **備註**:
    1. **OpenMetrics Agent 化**: Docker label Autodiscovery 與 scrape worker 移入 headless Agent lifecycle；Docker discovery 暫時失敗時保留 last-known-good targets，成功空掃描才清除 target set。
    2. **DogStatsD resource governance**: 對 series、packet line、metric/tag 長度、tag 數、histogram observations、set cardinality 加入顯式上限及 cumulative drop accounting，避免 UDP sender 造成無界記憶體增長。
    3. **Durable forwarding**: 已有 SQLite outbound queue / HTTP retry path 正式接入 headless Agent；forwarding 預設停用，支援 config hot reload、last-known-good config、bounded disk spool 與 runtime health snapshot。本機 SQLite persistence 成功後才 enqueue 遠端 spool，避免遠端失敗污染本機 persistence retry 語義。
    4. **All-container telemetry**: 新增 headless Docker container metrics worker，背景收集所有 running containers 的 CPU、memory、network bytes/rates、PID count，具有 max-container / point / byte bounds 與 per-container failure isolation。
    5. **Windows Service parity**: Windows Service host 現在組合與 console Agent 相同的 container / DogStatsD / OpenMetrics / forwarding 預設 runtime，避免 service mode 靜默缺少新能力。
    6. **Agent self-observability**: 將 persistence failures、DogStatsD drops、OpenMetrics discovery、container collector errors、forwarding queue/backoff health 轉成 `source=agent` custom metrics，週期性持久化並可選擇 forwarding；CLI status 同時顯示 self-metric progress/error。
    7. **驗證範圍**: GitHub Actions 已連續驗證 Windows/Linux × Python 3.11/3.13 的 hardware-free unit tests、`agent.py --help`、Windows Service host import smoke 及 compile checks。此環境沒有宣稱測過真實 Docker Desktop/daemon、Windows SCM service install/run、HWiNFO shared memory、NVIDIA driver/GPU 或其他實體硬件。
    8. **剩餘差距**: process/log/alert 的完整 headless lifecycle、shared Docker discovery/cache、collector/plugin lifecycle、一般化 config hot reload、transactional forwarding reconciliation、Explorer UX、installer/upgrade/release/performance/security 與真機 soak tests 仍需繼續。

## 2026-01-27
### 11:30 - 初始化項目
- **類型**: 創建
- **文件**: 文檔/LOG.md, container-profiler/
- **狀態**: ✅ 成功
- **備註**: 開始階段 1，建立項目骨架

### 12:00 - UI 完整佈局
- **類型**: 創建/修改
- **文件**: src/profiler/gui/*
- **狀態**: ✅ 成功
- **備註**: 完成階段 2，實現了容器列表、指標卡片、實時曲線和主窗口的整合。修復了 docstring 語法錯誤。

### 12:30 - 核心功能實現與驗證
- **類型**: 創建/修改
- **文件**: src/profiler/core/*, src/profiler/gui/main_window.py
- **狀態**: ✅ 成功
- **備註**: 完成階段 3, 4, 5。
    - 實現了 DockerMonitor (含 Mock 模式)
    - 實現了 PowerMonitor (含 Mock 模式)
    - 實現了 DataManager (CSV 導出)
    - 集成到 MainWindow，並通過 Mock 數據驗證了完整流程

### 12:45 - 應用打包 (初步)
- **類型**: 構建
- **文件**: dist/ContainerProfiler.exe
- **狀態**: ✅ 成功
- **備註**: 完成階段 6。PyInstaller 打包成功，生成獨立可執行文件。

### 13:15 - 功能修復與優化 (最終版)
- **類型**: 修復/優化
- **文件**: src/profiler/**
- **狀態**: ✅ 成功
- **備註**: 
    1. **HWiNFO**: 重寫 PowerMonitor，使用 Windows API 正確讀取共享內存，修復檢測失敗問題。
    2. **UI 優化**: 修復深色模式下圖表、下拉框文字不可見的問題；增強列表選中項對比度。
    3. **打包修復**: 修復 PyInstaller `pathex` 和 `hiddenimports` 配置，解決 ModuleNotFoundError。
    4. **模式切換**: 移除所有 Mock 數據，僅使用真實數據源。

### 13:45 - 高頻採樣支持與頻率控制
- **類型**: 功能新增 / UI 修復
- **文件**: src/profiler/gui/main_window.py, src/profiler/gui/chart_widget.py
- **狀態**: ✅ 成功
- **備註**: 
    1. **頻率控制**: 在主界面添加了 QSpinBox，支持 20ms - 1000ms 自由調節採樣頻率。
    2. **高頻優化**: 將圖表緩衝區提升至 3000 點，確保高頻採樣下曲線流暢且具備足夠歷史長度。
    3. **UI 視覺**: 徹底修復了實時監控曲線旁邊選項（QComboBox）在深色模式下的「白底白字」問題。
    4. **打包成功**: 刪除了舊的 `dist/` 目錄並重新運行 PyInstaller，成功生成最新 `ContainerProfiler.exe` (約 51 MB)。

### 17:15 - 最終打包完成
- **類型**: 構建
- **文件**: dist/ContainerProfiler.exe
- **狀態**: ✅ 成功
- **備註**: 項目所有階段已完成，高頻採樣功能已集成並成功打包。

### 18:00 - 打包問題修復與依賴補全
- **類型**: 修復/構建
- **文件**: main.py, build.spec, dist/ContainerProfiler_v2.exe
- **狀態**: ✅ 成功
- **備註**: 
    1. **代碼修復**: 重寫 `main.py` 修復了文件編碼導致的路徑設置無效問題。
    2. **模塊丟失修復**: 在 `build.spec` 中使用 `datas` 強制包含 `profiler` 源碼包，解決 `ModuleNotFoundError: No module named 'profiler'`。
    3. **依賴補全**: 在 `hiddenimports` 中顯式添加 `docker`, `pynvml`, `construct` 等第三方庫，解決運行時依賴丟失。
    4. **文件鎖定規避**: 輸出文件重命名為 `ContainerProfiler_v2.exe` 以避開系統對舊文件的鎖定。
    5. **最終產物**: `dist/ContainerProfiler_v2.exe` (已包含完整依賴)。

### 18:30 - 圖表優化與性能改進
- **類型**: 功能增強 / 性能優化
- **文件**: src/profiler/gui/chart_widget.py, src/profiler/gui/main_window.py, dist/ContainerProfiler_v4.exe
- **狀態**: ✅ 成功
- **備註**:
    1. **圖表自動縮放 (v3)**: 在圖表區新增 `Auto Scale` 選項，啟用後根據數據實時波動自動調整 Y 軸範圍，解決小數值波動看不清的問題。
    2. **異步採集架構 (v4)**: 引入 `WorkerThread` 後台線程，將 Docker/HWiNFO 數據採集與 UI 主線程分離。徹底解決了「監控開啟後界面卡頓」的問題，確保 UI 在高頻採樣下依然流暢響應。
    3. **最終產物**: `dist/ContainerProfiler_v4.exe` (推薦使用版本)。
