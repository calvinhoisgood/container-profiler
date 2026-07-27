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
