# Container Profiler

只用於個人保存。

Container Profiler v5 正從單一 Windows Docker 桌面監控器重構成一個自包含、agent-style 的 observability runtime。長期設計方向對標 Datadog Agent / Container Explorer：核心採集盡量直接使用 Docker、作業系統與硬件廠商提供的原生接口，而不是要求另一套監控軟件先替本程式採數據。

## 目前架構

```text
Docker Engine ───────> container / process / logs / labels / network context ──┐
Windows Kernel32 ────> native host CPU / memory / uptime                       │
Linux /proc ─────────> native host CPU / memory / load / uptime                ├─> normalized telemetry
NVIDIA NVML ─────────> GPU utilization / memory / temperature / power           │
DogStatsD UDP ───────> custom metrics                                           │
OpenMetrics HTTP <───> Docker-label Autodiscovery + bounded scrape worker ──────┘
                                      │
                                      ├─> threshold alert engine
                                      ├─> bounded in-memory buffers
                                      ├─> SQLite WAL local store
                                      └─> Session / Alert / Workload / Custom Metrics Explorer
```

主要工程特性：

- Docker CPU、working-set-style memory、network delta/rate 採集，保留 unavailable (`None`) 與真實 0 的差別。
- 固定 deadline 採樣、collection latency / scheduling lag、failure / skipped-tick 自監控。
- SQLite WAL session/sample persistence、bounded query、Session Explorer、百分位與能耗摘要。
- Docker unified-service tags、Compose tags、事件式容器 discovery、debounce 與 reconnect backoff。
- Process Explorer、bounded log snapshot、live log follow、line/byte queue bound、drop accounting 與取消。
- DogStatsD parser/UDP listener，以及 OpenMetrics text parser/HTTP collector。
- Datadog-style Docker OpenMetrics Autodiscovery labels；支援 `%%host%%`、named-network host、port template、PID/hostname context，並對 label-driven HTTP target 做安全限制。
- OpenMetrics scrape 在背景 runtime 執行，經 normalized custom metric pipeline、bounded backpressure buffer、SQLite retention，再進 Custom Metrics Explorer。
- threshold alert rules、sustained trigger、recovery hysteresis、JSON hot reload / last-known-good，以及持久化 Alert Explorer + acknowledgement。
- Windows/Linux × Python 3.11/3.13 的 hardware-free CI。

## 不依賴 HWiNFO 才能工作

HWiNFO 不再是預設 runtime dependency。核心 host metrics 走 Windows Kernel32 或 Linux `/proc`；GPU 走 NVIDIA 官方 NVML 接口。HWiNFO 只保留為「CPU Package Power」的可選 compatibility backend。

若確實要使用 HWiNFO CPU Package Power，可在啟動前明確開啟：

```powershell
$env:CONTAINER_PROFILER_ENABLE_HWINFO="1"
python main.py
```

沒有 HWiNFO 時，`cpu_power_w` 為 unavailable，但 Docker、host、GPU、logs、alerts、DogStatsD/OpenMetrics 等路徑不應因此失效。

## 運行環境

- Windows 10/11 為主要桌面目標；核心模組與 CI 同時支援 Linux。
- Python 3.11+
- Docker Desktop / Docker Engine：容器監控所需。
- NVIDIA driver / NVML：只在需要 NVIDIA GPU telemetry 時才需要。
- HWiNFO64：只有使用可選 CPU Package Power compatibility backend 時才需要。

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

## 數值語義

GUI 的採樣週期是目標週期，不是硬實時保證。分析應以實際 `timestamp` / `elapsed_s` 為準。第一個 network sample 沒有 delta baseline，因此 rate 是 `None` 而不是偽造 0；optional sensor 同樣遵守這個規則。

CSV session telemetry 目前包含：

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

OpenMetrics custom metrics 與 alert transitions 另外保存於本機 SQLite，避免把高 cardinality series 強塞進固定 container sample schema。

## 測試

不需要 Docker daemon、HWiNFO 或 NVIDIA GPU 即可執行完整 hardware-free suite：

```powershell
cd container-profiler
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q src
```

GitHub Actions 會在 Windows/Linux × Python 3.11/3.13 上跑同一套測試。這些測試驗證 parsing、scheduling、storage、backpressure、failure semantics 與 native API adapter 邏輯，但不能替代實際 Docker Desktop / GPU / HWiNFO 硬件冒煙測試。

## 仍在推進的 Datadog 能力差距

目前仍未等價於 Datadog Agent。後續重點包括：更完整的 host disk/IO/network/process checks、native Windows service/agent 化、可靠 forwarding + disk spool、配置中心與 collector/plugin lifecycle、custom metric/tag query UX、logs pipeline、service checks、更多 GPU/硬件後端、安全與性能 hardening、Windows installer / upgrade / release validation。

本分支的原則不是堆 GUI 功能，而是逐步建立可以長期運行、可觀測、資源有界、故障可降級、可測試的 agent runtime。
