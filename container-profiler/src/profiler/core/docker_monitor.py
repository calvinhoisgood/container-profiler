"""
Docker 容器監控模組 - 僅限真實數據
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
    created: str

@dataclass
class ContainerStats:
    """容器資源統計"""
    timestamp: float
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
            print(f"Docker 連接失敗: {e}")
    
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
        if not self.is_connected():
            self._connect()
            if not self.client:
                return []
        
        try:
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
        except Exception as e:
            print(f"獲取容器列表失敗: {e}")
            return []
    
    def get_stats(self, container_id: str) -> Optional[ContainerStats]:
        """獲取容器即時統計"""
        if not self.client:
            return None
        
        try:
            container = self.client.containers.get(container_id)
            stats = container.stats(stream=False)
            
            # 1. 計算 CPU 使用率
            cpu_stats = stats["cpu_stats"]
            precpu_stats = stats["precpu_stats"]
            
            cpu_usage = cpu_stats["cpu_usage"]["total_usage"]
            precpu_usage = precpu_stats["cpu_usage"]["total_usage"]
            
            system_cpu_usage = cpu_stats.get("system_cpu_usage", 0)
            presystem_cpu_usage = precpu_stats.get("system_cpu_usage", 0)
            
            cpu_delta = cpu_usage - precpu_usage
            system_delta = system_cpu_usage - presystem_cpu_usage
            
            cpu_count = cpu_stats.get("online_cpus", 1)
            
            cpu_percent = 0.0
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0
            
            # 2. 內存
            memory_stats = stats.get("memory_stats", {})
            memory_usage = memory_stats.get("usage", 0)
            memory_limit = memory_stats.get("limit", 1)
            
            # 3. 網絡
            networks = stats.get("networks", {})
            rx_bytes = sum(v.get("rx_bytes", 0) for v in networks.values())
            tx_bytes = sum(v.get("tx_bytes", 0) for v in networks.values())
            
            return ContainerStats(
                timestamp=datetime.now().timestamp(),
                cpu_percent=round(cpu_percent, 2),
                memory_mb=round(memory_usage / 1024 / 1024, 2),
                memory_limit_mb=round(memory_limit / 1024 / 1024, 2),
                network_rx_bytes=rx_bytes,
                network_tx_bytes=tx_bytes
            )
        except Exception as e:
            print(f"獲取統計失敗: {e}")
            return None