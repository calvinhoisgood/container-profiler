"""
測試 DockerMonitor 核心功能
"""
import sys
from pathlib import Path
import time

# 添加 src 到路徑
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

from profiler.core.docker_monitor import DockerMonitor

def test_monitor():
    print("初始化監控器...")
    monitor = DockerMonitor()
    
    if not monitor.is_connected():
        print("錯誤: 無法連接 Docker。請確保 Docker Desktop 已啟動。")
        return

    print("Docker 連接成功！")
    
    print("\n獲取容器列表:")
    containers = monitor.list_containers()
    for c in containers:
        print(f"- [{c.status}] {c.name} ({c.id[:8]})")
        
    if not containers:
        print("沒有發現容器。請先啟動一個容器進行測試。")
        return

    target_id = containers[0].id
    print(f"\n測試監控容器: {containers[0].name} (持續 5 秒)")
    
    for _ in range(5):
        stats = monitor.get_stats(target_id)
        if stats:
            print(f"Time: {stats.timestamp:.0f} | CPU: {stats.cpu_percent}% | Mem: {stats.memory_mb}MB")
        time.sleep(1)

if __name__ == "__main__":
    test_monitor()
