"""Qt adapter for event-driven Docker container discovery."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.docker_monitor import DockerMonitor


class ContainerDiscoveryThread(QThread):
    """Bridge the blocking Docker event stream into Qt signals."""

    container_event = pyqtSignal(object)
    watcher_error = pyqtSignal(str)

    def __init__(self, monitor: DockerMonitor) -> None:
        super().__init__()
        self.monitor = monitor
        self.watcher = monitor.create_event_watcher()

    def run(self) -> None:
        if self.watcher is None:
            return
        self.watcher.run(self.container_event.emit)
        if self.watcher.last_error:
            self.watcher_error.emit(self.watcher.last_error)

    def stop(self, timeout_ms: int = 3000) -> bool:
        if self.watcher is not None:
            self.watcher.stop()
        return self.wait(timeout_ms)
