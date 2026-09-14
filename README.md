# Container Profiler

只用於個人保存。

面向深度學習／推理負載的 Windows Docker 桌面監控工具。v5 不再只是往 v4 疊功能，而是把採樣核心重新拆成可測試的資料模型、Docker 採樣、功耗採樣、資料記錄與 GUI 幾層，優先修正數值語義、平台耦合和缺乏測試的問題。

## v5 主要改動

- Docker SDK 改為延遲載入，因此沒有 Docker 的機器仍可導入核心模組和跑單元測試。
- CPU 使用率兼容 `online_cpus` 缺失；內存改為優先扣除可回收 `inactive_file`，更接近容器工作集語義。
- 網絡不再把累積 bytes 當成速率；現在基於相鄰採樣計算 `network_rx_bps` / `network_tx_bps`。
- `None` 明確代表「傳感器不可用」，不再與真實的 `0 W` / `0 %` 混為一談；CSV 同樣保留差別。
- HWiNFO Windows API 不再於 import 階段初始化，因此非 Windows 平台可安全導入；共享內存讀取加入元素大小／數量保護。
- NVML 支持依賴注入，沒有 NVIDIA GPU 也能做確定性單元測試。
- 採樣線程改用 Qt interruption 機制並分段睡眠，停止監控／退出更容易及時響應。
- 圖表不再把 CPU 百分比和 Memory MB 放到同一 Y 軸；改為 CPU/Memory%、Power、GPU%、Network MiB/s 四個模式。
- CSV 新增 elapsed、內存百分比、網絡速率、PID、GPU 總顯存與溫度等欄位。
- 新增硬件無關單元測試，以及 Windows/Linux、Python 3.11/3.13 的 GitHub Actions 測試矩陣。

## 數據流

```text
Docker Engine ──> DockerMonitor ──────┐
                                      ├─> WorkerThread ─> UI / Chart ─> DataManager ─> CSV
HWiNFO Shared Memory ─> HWiNFOReader ─┤
NVIDIA NVML ──────────> NVMLReader ───┘
```

`ContainerStats` 和 `PowerStats` 是核心資料契約。GUI 只消費結構化採樣，不直接碰 Docker、HWiNFO 或 NVML。

## 運行環境

- Windows 10/11
- Python 3.11+
- Docker Desktop（容器監控必需）
- HWiNFO64，並啟用 Sensors-only / Shared Memory Support（CPU Package Power，可選）
- NVIDIA 驅動及 NVML 支持顯卡（GPU 指標，可選）

HWiNFO 或 NVIDIA GPU 缺失時，對應欄位顯示為不可用，其餘容器監控仍可工作。

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

## 採樣週期的含義

GUI 裡的採樣週期是「目標週期」，不是硬實時保證。每輪先完成 Docker 和功耗採樣，再等待剩餘時間；如果一次底層採樣已經超過目標週期，下一輪會立即開始而不額外等待。因此後續分析應以 CSV 的真實 `timestamp` / `elapsed_s` 為時間基準。

## CSV 欄位

```text
timestamp, elapsed_s, container_id,
cpu_percent,
memory_mb, memory_limit_mb, memory_percent,
network_rx_bytes, network_tx_bytes, network_rx_bps, network_tx_bps,
pids,
cpu_power_w,
gpu_power_w, gpu_util_percent,
gpu_memory_mb, gpu_memory_total_mb, gpu_temp_c
```

第一個網絡採樣沒有前一個基線，因此速率欄位留空，而不是偽造為 0。

## 測試

無 Docker、無 HWiNFO、無 NVIDIA GPU 也能跑核心單元測試：

```powershell
cd container-profiler
python -m unittest discover -s tests -p "test_*_unit.py" -v
python -m compileall -q src tests
```

有 Docker Desktop 和運行中容器時，再執行真實環境冒煙測試：

```powershell
python tests/test_docker_core.py
```

## 已知限制

- HWiNFO CPU 功耗仍依賴 Windows 共享內存及 HWiNFO 提供的欄位名稱。
- GPU 指標目前仍只有 NVIDIA NVML，尚未加入 AMD／Intel GPU 後端。
- 無硬件單元測試能驗證計算、解析、缺失依賴與 CSV 語義，但不能替代 Windows + Docker Desktop + HWiNFO + NVIDIA 的完整實機驗證。
- Docker 採樣延遲由 Docker Engine／宿主環境決定；把 GUI 設成 20 ms 不代表 Engine 真能提供 50 Hz 的新鮮統計。

## 項目背景

本項目服務於「面向深度學習負載的容器運行時畫像與工具集構建」畢業設計。v5 的重點不是讓 GUI 看起來更複雜，而是讓採樣結果可以被解釋、重現、測試，並且在傳感器缺失或平台不同時清楚地失敗。
