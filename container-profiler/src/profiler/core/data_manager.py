"""
數據管理模組 - 負責數據收集、緩存和導出
"""
import csv
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

class DataManager:
    def __init__(self):
        self.recorded_data: List[Dict[str, Any]] = []
        self.is_recording = False
        self.start_time = None
        
    def start_recording(self):
        """開始記錄"""
        self.recorded_data = []
        self.is_recording = True
        self.start_time = datetime.now()
        
    def stop_recording(self):
        """停止記錄"""
        self.is_recording = False
        
    def add_record(self, container_id, stats, power_stats):
        """添加一條記錄"""
        if not self.is_recording:
            return
            
        record = {
            "timestamp": datetime.fromtimestamp(stats.timestamp).isoformat(),
            "container_id": container_id,
            "cpu_percent": stats.cpu_percent,
            "memory_mb": stats.memory_mb,
            "network_rx": stats.network_rx_bytes,
            "network_tx": stats.network_tx_bytes,
            "cpu_power_w": power_stats.cpu_power_w if power_stats.cpu_power_w else 0,
            "gpu_power_w": power_stats.gpu_power_w if power_stats.gpu_power_w else 0,
            "gpu_util": power_stats.gpu_util_percent if power_stats.gpu_util_percent else 0,
            "gpu_memory_mb": power_stats.gpu_memory_mb if power_stats.gpu_memory_mb else 0
        }
        self.recorded_data.append(record)
        
    def export_csv(self, file_path: str) -> bool:
        """導出為 CSV"""
        if not self.recorded_data:
            return False
            
        try:
            fieldnames = [
                "timestamp", "container_id", "cpu_percent", "memory_mb", 
                "network_rx", "network_tx", 
                "cpu_power_w", "gpu_power_w", "gpu_util", "gpu_memory_mb"
            ]
            
            # 確保目錄存在
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            
            with open(file_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.recorded_data)
                
            return True
        except Exception as e:
            print(f"導出 CSV 失敗: {e}")
            return False
