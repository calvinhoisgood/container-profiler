# Container Profiler - AI 助手工作流規範

> **目的**: 確保每次 AI 助手接手都能保持一致的開發體驗

---

## 一、項目概覽

### 快速了解

| 項目 | 說明 |
|:-----|:-----|
| **論文題目** | 面向深度學習負載的容器運行時畫像與工具集構建 |
| **核心功能** | 監控 Docker 容器的 CPU/GPU 功耗、資源使用 |
| **使用模式** | CLI / SDK / Web Dashboard |
| **目標平台** | Windows (HWiNFO)，可選 GPU (NVML) |

### 關鍵文檔位置

```
D:\JNUFINAL\
├── 文檔\
│   ├── DESIGN.md           ← 設計文檔 (架構設計)
│   ├── QUICKSTART.md       ← 快速開始指南
│   ├── WORKFLOW_PROMPT.md  ← 完整開發工作流
│   └── LOG.md              ← 開發日誌 (每次操作必更新)
│
└── container-profiler\
    ├── README.md           ← 項目說明 (結構說明)
    └── docs\PACKAGING.md   ← exe 封裝規劃
```

---

## 二、接手前必讀

每次開始工作前，請按順序閱讀：

1. **`文檔/LOG.md`** - 查看最新進度和待辦事項
2. **`container-profiler/README.md`** - 了解項目結構
3. **當前對話歷史** - 了解用戶最近的需求

---

## 三、開發規範

### 3.1 日誌記錄

**每次操作後必須更新 `文檔/LOG.md`**:

```markdown
### HH:MM - 操作標題
- **類型**: 創建/修改/刪除/測試/配置
- **文件**: 相關文件路徑
- **狀態**: ✅ 成功 / ❌ 失敗 / ⏳ 進行中
- **備註**: 補充說明（可選）
```

### 3.2 代碼風格

```python
# 1. 資源路徑必須使用動態獲取
from pathlib import Path
import sys

def get_resource_path(relative_path: str) -> Path:
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent.parent
    return base_path / relative_path

# 2. 可選依賴使用延遲導入
def get_gpu_info():
    try:
        import pynvml
        # ...
    except ImportError:
        return None

# 3. 配置文件路徑遵循優先級
# 當前目錄 > 用戶目錄 > 默認配置
```

### 3.3 目錄結構

```
container-profiler/
├── docker/demo/          ← 示範容器
├── profiler/core/        ← 核心監控 (最重要)
├── profiler/web/         ← Web Dashboard
├── profiler/cli/         ← CLI 工具
├── tests/                ← 測試腳本
├── docs/                 ← 文檔
├── examples/             ← 使用示例
└── output/               ← 輸出 (gitignore)
```

---

## 四、常見操作

### 4.1 創建新文件

1. 在對應目錄創建文件
2. 更新 `LOG.md`
3. 如果是新模組，更新 `README.md` 說明

### 4.2 運行測試

```powershell
cd D:\JNUFINAL\container-profiler

# 運行示範容器測試
python tests/test_demo_container.py

# 啟動 Web Dashboard
python profiler.py web --port 8080
```

### 4.3 構建 Docker 鏡像

```powershell
cd D:\JNUFINAL\container-profiler\docker\demo
docker build -t demo-ai-server:v1 .
```

---

## 五、決策記錄

| 日期 | 決策 | 原因 |
|:-----|:-----|:-----|
| 2026-01-25 | 使用 PyInstaller 打包 | 成熟穩定，文檔豐富 |
| 2026-01-25 | 先做 CLI 再做 GUI | 降低複雜度 |
| 2026-01-25 | 項目放在 JNUFINAL 下 | 用戶工作區限制 |
| 2026-01-26 | LLM 容器使用 vLLM | 替代 Transformers+AutoGPTQ，解決編譯和穩定性問題 |
| 2026-01-26 | 顯存監控優化 | Qwen3 容器顯存預留設為 30% 以適應桌面環境 |

---

## 六、待確認問題

> ⚠️ 遇到以下問題時，必須先詢問用戶

1. **環境相關**
   - 用戶是否有 NVIDIA GPU？
   - HWiNFO64 是否已安裝並啟用共享內存？

2. **設計相關**
   - Web Dashboard 是否需要用戶認證？
   - 數據導出格式偏好 (CSV/JSON/兩者)？

3. **優先級相關**
   - 遇到多個待辦任務時，詢問優先順序

---

## 七、禁止事項

❌ **不要做的事**:

1. 不要在沒有更新日誌的情況下結束操作
2. 不要硬編碼文件路徑
3. 不要跳過測試直接進入下一階段
4. 不要在不確定用戶意圖時自行決定

---

## 八、版本歷史

| 版本 | 日期 | 變更 |
|:-----|:-----|:-----|
| v1.0 | 2026-01-25 | 初始版本 |
| v1.1 | 2026-01-26 | 完成 Qwen3 示範容器開發 (vLLM) |
