# Container Profiler

只用於個人保存。

Container Profiler v5 正從單一 Windows Docker 桌面監控器重構成自包含、agent-style 的 observability runtime。長期設計方向參考 Datadog Agent / Container Explorer 的核心能力與工程成熟度，但不複製專有程式碼，也不宣稱功能等價。核心採集優先直接使用 Docker、作業系統、kernel 與硬件廠商提供的接口，而不是要求另一套監控軟件先替本程式採數據。

## 目前架構

```text
Docker Engine ───────> all-container CPU / memory / network / PIDs ─────────────┐
                 └──> labels / network context -> OpenMetrics Autodiscovery     │
Windows Kernel32 ────> native host CPU / memory / uptime                       │
Linux /proc ─────────> native host CPU / memory / load / uptime                │
Tool Help + /proc ───> bounded native process / thread summary                  ├─> normalized telemetry
native OS APIs ──────> filesystem / disk I/O / network                         │
NVIDIA NVML ─────────> GPU utilization / memory / temperature / power           │
DogStatsD UDP ───────> bounded custom metric aggregation                        │
OpenMetrics HTTP <───> bounded background scrape worker                         │
Agent internals ─────> health / drop / failure / queue self-metrics ────────────┘
                                      │
                                      ├─> SQLite WAL local telemetry store
                                      ├─> bounded retention + query
                                      ├─> per-series threshold alerts + audit events
                                      ├─> durable outbound SQLite spool -> HTTP forwarding
                                      └─> Session / Alert / Workload / Custom Metrics Explorer
```

目前 headless Agent 已直接擁有主要長期運行路徑，而不是依附 GUI lifecycle：

- 原生 Windows/Linux host CPU、memory、uptime、filesystem、network、disk I/O checks；rate counter reset、第一筆無 baseline、partial failure 都保留明確語義。
- 原生 host process summary 不依賴 psutil/WMI/HWiNFO：Linux 直接讀 `/proc/<pid>/stat`，Windows 使用 Kernel32 Tool Help snapshot；兩者提供 process/thread totals，Linux 額外提供 running/sleeping/blocked/stopped/zombie state breakdown，且 transient process-exit race 只記 skipped count，不令整輪採集失敗。
- 所有 running Docker containers 的 CPU、working-set-style memory、network bytes/rates 與 PID count 背景採集，寫入 shared custom-metric store；單一容器失敗不會丟掉同輪其他容器的成功樣本。
- Docker unified-service / Compose tags、容器 labels、network/PID/hostname context，以及 Datadog-style OpenMetrics Autodiscovery template resolution。
- OpenMetrics Autodiscovery 與 scrape worker 已由 headless Agent lifecycle 擁有；Docker 暫時不可用時保留 last-known-good targets，成功的空 discovery 才會清除 targets。
- 本機 DogStatsD 預設只 bind `127.0.0.1:8125`，支援 gauge/count/set/histogram；series、line/tag、histogram observation、set cardinality 與 persistence buffer 均有顯式上限及 drop accounting。
- SQLite WAL 保存 host/custom metrics，具 bounded query、retention，以及 Session / Alert / Custom Metrics Explorer。
- 可選 headless custom-metric threshold alerts 支援任意 exact metric name、sustained trigger、recovery hysteresis、severity、bounded per-series state，以及 config hot reload；無效 replacement 保留 last-known-good rules/state，成功 replacement 會清除舊 threshold state，避免套用新閾值時沿用舊 pending/firing 狀態。Metric 先成功寫入 SQLite 才進行 alert evaluation，alert audit sink failure 不會造成 telemetry duplicate/requeue。
- 遠端 forwarding 預設停用；啟用後使用 bounded SQLite disk spool、at-least-once delivery、retry/backoff/jitter。`forwarding.json` 可 hot reload，無效 replacement 會保留 last-known-good runtime。
- Agent 會定期把自身 persistence failures、native system-check partial failures、DogStatsD drops、OpenMetrics discovery、container collection、alert cardinality/eviction/config health 與 forwarding queue health 寫回 custom metrics，因此 Explorer 與 forwarding 都能觀察 Agent 本身。
- Windows Service host 使用與 console Agent 相同的預設 container / DogStatsD / OpenMetrics / forwarding runtime 組合；服務控制不要求 pywin32。Custom-metric alerts 目前是 console Agent 的明確 CLI opt-in，尚未宣稱 service install CLI 已完整配置該選項。
- Process Explorer、bounded Docker log snapshot/live-follow、原有 session alert engine、alert audit/acknowledgement 與 Explorer UI 已有獨立模組；host process aggregate 與 generic metric alerts 已進入 headless lifecycle，但 richer per-process inventory 與 logs 的完整 headless persistence/query pipeline 仍在推進。
- Windows/Linux × Python 3.11/3.13 的 hardware-free CI 驗證 parsing、scheduling、storage、failure semantics、backpressure、service imports 與 source compilation；native process smoke 亦在對應 Windows/Linux runner 上直接呼叫 Tool Help 或 `/proc`。

## 不依賴 HWiNFO 才能工作

HWiNFO 不再是核心 runtime dependency。核心 host metrics 走 Windows Kernel32、Linux `/proc` 或其他 OS-native adapters；GPU 走 NVIDIA 官方 NVML 接口。HWiNFO 只保留為 CPU Package Power 的可選 compatibility backend。

若確實要使用 HWiNFO CPU Package Power，可在啟動桌面程式前明確開啟：

```powershell
$env:CONTAINER_PROFILER_ENABLE_HWINFO="1"
python main.py
```

沒有 HWiNFO 時，`cpu_power_w` 為 unavailable，但 Docker、host、GPU、logs、alerts、DogStatsD/OpenMetrics 等其他路徑不應因此失效。

## 運行環境

- Windows 10/11 為主要桌面與 Service 目標；核心模組與 CI 同時支援 Linux。
- Python 3.11+
- Docker Desktop / Docker Engine：只有 container / Docker Autodiscovery 功能需要。
- NVIDIA driver / NVML：只有 NVIDIA GPU telemetry 需要。
- HWiNFO64：只有啟用可選 CPU Package Power compatibility backend 時需要。

## 快速開始

桌面 Explorer：

```powershell
git clone https://github.com/calvinhoisgood/container-profiler.git
cd container-profiler\container-profiler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

headless Agent：

```powershell
cd container-profiler
python agent.py
python agent.py --status
```

常用 Agent 開關包括 `--no-container-metrics`、`--no-dogstatsd`、`--no-openmetrics`、`--no-forwarding`，以及各 collector / heartbeat / self-metric interval。遠端 forwarding 不會因為啟動 Agent 而自動開啟，必須由有效的 forwarding config 明確 enable。

Custom-metric alerts 也是 opt-in。倉庫提供的 example rules 預設全部 `enabled: false`，先依實際 workload 調整 threshold 再啟用：

```powershell
python agent.py --metric-alerts-config config/metric-alerts.example.json
python agent.py --status
```

Alert state 以 rule + target/container/tags 隔離，並由 `--metric-alerts-max-series` 限制可追蹤 series 數量，避免高 cardinality metric 讓 agent memory 無界增長。配置檔可 hot reload；CLI status 會顯示 rule/series/eviction 與 config error 狀態。

Windows Service 安裝/刪除屬於持久 machine-state 變更，因此 CLI 要求額外確認旗標；可以先安全地查看將註冊的命令：

```powershell
python agent_service.py print-install-command
python agent_service.py install --confirm-install
python agent_service.py query
```

## 數值與可靠性語義

採樣週期是目標 cadence，不是硬實時保證；分析應以實際 timestamps 為準。第一個 network sample 沒有 delta baseline，因此 rate 是 `None` 而不是偽造 `0`；optional sensor 同樣遵守 unavailable 與真實零值分離的原則。

Custom metrics 先寫本機 SQLite，再嘗試放入獨立 durable forwarding spool，所以本機 durability 不依賴網絡。spool 內的 delivery 採 at-least-once 語義；local telemetry commit 與另一個 SQLite spool enqueue 目前不是單一跨資料庫 transaction，因此尚未宣稱 crash-window 下的端到端 exactly-once 或 transactional-outbox 保證。

Alert evaluation 也刻意放在 metric local commit 之後，因此 metric storage failure 不會推進 threshold state；目前 alert transition 寫入 audit table 與 metric commit 仍不是單一 SQLite transaction，alert-event sink failure 會被 self-monitoring 記錄但不會回滾已持久化 metric。這個 crash/failure window 尚未宣稱 exactly-once alert auditing。

目前 all-container metrics 與 OpenMetrics discovery 各自使用獨立 lazy Docker monitor/client，避免彼此故障耦合；未來仍可進一步整合 shared discovery/cache 以降低 daemon round trips。

## 測試

不需要 Docker daemon、HWiNFO 或 NVIDIA GPU 即可執行 hardware-free suite：

```powershell
cd container-profiler
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q src
python agent.py --help
python agent_service.py --help
```

GitHub Actions 會在 Windows/Linux × Python 3.11/3.13 上跑同一套核心驗證。這些測試不能替代真實 Docker Desktop、Windows Service Control Manager、NVIDIA GPU 或 HWiNFO shared-memory 冒煙測試；沒有實際執行過的硬件路徑不應被描述為已驗證。

## 仍在推進的 Datadog 能力差距

目前仍遠未等價於 Datadog Agent。後續高價值工作包括：richer host/container per-process inventory 與 process query UX、logs 的 headless persistence/query/backpressure pipeline、alert tag/source selectors 與更可靠的 alert-event retry/retention、service checks、collector/plugin lifecycle 與更一般化的 Agent config hot reload、shared Docker discovery/cache、更細緻的 tag/cardinality governance、Explorer query/filter/search/export UX、transactional forwarding reconciliation、安全/性能壓測、Windows installer/upgrade/release validation，以及真實 Windows + Docker + NVIDIA/HWiNFO 環境的 smoke / soak testing。

本分支的原則不是堆 GUI 功能，而是逐步建立可以長期運行、可觀測、資源有界、故障可降級、可測試且可以安全演進的 agent runtime。