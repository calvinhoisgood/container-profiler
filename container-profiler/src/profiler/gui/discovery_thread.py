"""Qt adapter for resilient event-driven Docker container discovery."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.discovery import ReconnectBackoff
from ..core.docker_monitor import DockerMonitor


class ContainerDiscoveryThread(QThread):
    """Bridge Docker's blocking event stream into Qt signals.

    Docker Desktop restarts, daemon upgrades, and transient socket failures can
    terminate the event stream. The thread recreates the watcher with bounded
    exponential backoff and resets that backoff after a stream successfully
    delivers an event.
    """

    container_event = pyqtSignal(object)
    watcher_error = pyqtSignal(str)
    watcher_state = pyqtSignal(str)

    def __init__(self, monitor: DockerMonitor) -> None:
        super().__init__()
        self.monitor = monitor
        self.watcher = None

    def _wait_interruptibly(self, seconds: float) -> bool:
        """Return False when interrupted while waiting."""
        remaining_ms = max(0, int(seconds * 1000.0))
        while remaining_ms > 0:
            if self.isInterruptionRequested():
                return False
            chunk = min(100, remaining_ms)
            self.msleep(chunk)
            remaining_ms -= chunk
        return not self.isInterruptionRequested()

    def run(self) -> None:
        backoff = ReconnectBackoff(initial_s=0.5, maximum_s=30.0, factor=2.0)

        while not self.isInterruptionRequested():
            watcher = self.monitor.create_event_watcher()
            self.watcher = watcher
            if watcher is None:
                detail = self.monitor.last_error or "Docker event stream unavailable"
                self.watcher_error.emit(detail)
                delay = backoff.next_delay()
                self.watcher_state.emit(f"reconnecting in {delay:g}s")
                if not self._wait_interruptibly(delay):
                    break
                continue

            delivered = False

            def deliver(event) -> None:
                nonlocal delivered
                delivered = True
                backoff.reset()
                self.container_event.emit(event)

            self.watcher_state.emit("connected")
            watcher.run(deliver)
            self.watcher = None

            if self.isInterruptionRequested():
                break

            if watcher.last_error:
                self.watcher_error.emit(watcher.last_error)
            else:
                self.watcher_error.emit("Docker event stream ended; reconnecting")

            # If the stream had delivered events, reset() above makes the next
            # retry fast. Repeated connect failures still back off.
            delay = backoff.next_delay()
            if delivered:
                self.watcher_state.emit(f"reconnecting in {delay:g}s")
            else:
                self.watcher_state.emit(f"reconnecting in {delay:g}s")
            if not self._wait_interruptibly(delay):
                break

        self.watcher_state.emit("stopped")

    def stop(self, timeout_ms: int = 3000) -> bool:
        self.requestInterruption()
        watcher = self.watcher
        if watcher is not None:
            watcher.stop()
        return self.wait(timeout_ms)
