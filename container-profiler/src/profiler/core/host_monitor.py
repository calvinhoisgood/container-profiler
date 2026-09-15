"""Dependency-free native host CPU/memory/uptime collection.

Datadog's core host checks are built into the Agent rather than delegated to a
third-party monitoring application. This module establishes the same boundary:
Windows reads Kernel32 directly, Linux reads procfs, and HWiNFO remains only an
optional power-sensor provider elsewhere in the application.
"""
from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Protocol

_MIB = 1024.0 * 1024.0


@dataclass(frozen=True, slots=True)
class HostRawSample:
    timestamp: float
    cpu_total: int
    cpu_idle: int
    memory_total_bytes: int
    memory_available_bytes: int
    uptime_s: float
    logical_cpus: int
    load_1: float | None = None
    load_5: float | None = None
    load_15: float | None = None


@dataclass(frozen=True, slots=True)
class HostStats:
    timestamp: float
    cpu_percent: float | None
    logical_cpus: int
    memory_total_mb: float
    memory_available_mb: float
    memory_used_mb: float
    memory_percent: float
    uptime_s: float
    load_1: float | None = None
    load_5: float | None = None
    load_15: float | None = None


@dataclass(frozen=True, slots=True)
class HostWorkerSnapshot:
    running: bool
    samples_collected: int
    failed_samples: int
    queued_samples: int
    dropped_samples: int
    last_error: str | None
    last_stats: HostStats | None


class HostBackend(Protocol):
    def read(self) -> HostRawSample: ...


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


class WindowsNativeHostBackend:
    """Collect basic host metrics directly through documented Kernel32 APIs."""

    def __init__(
        self,
        *,
        kernel32: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
        cpu_count: Callable[[], int | None] = os.cpu_count,
    ) -> None:
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("Windows native host backend is only available on Windows")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetSystemTimes.argtypes = [
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            kernel32.GetSystemTimes.restype = ctypes.c_int
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
            kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
            kernel32.GetTickCount64.argtypes = []
            kernel32.GetTickCount64.restype = ctypes.c_uint64
        self.kernel32 = kernel32
        self.wall_clock = wall_clock
        self.cpu_count = cpu_count

    def read(self) -> HostRawSample:
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not self.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes failed")

        memory = _MEMORYSTATUSEX()
        memory.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")

        # Microsoft documents KernelTime as including IdleTime, so total CPU
        # ticks are KernelTime + UserTime while idle remains a subset of total.
        cpu_total = _filetime_value(kernel) + _filetime_value(user)
        cpu_idle = _filetime_value(idle)
        return HostRawSample(
            timestamp=float(self.wall_clock()),
            cpu_total=cpu_total,
            cpu_idle=cpu_idle,
            memory_total_bytes=int(memory.ullTotalPhys),
            memory_available_bytes=int(memory.ullAvailPhys),
            uptime_s=float(self.kernel32.GetTickCount64()) / 1000.0,
            logical_cpus=max(1, int(self.cpu_count() or 1)),
        )


class LinuxProcHostBackend:
    """Collect basic Linux host metrics from procfs without psutil."""

    def __init__(
        self,
        *,
        reader: Callable[[str], str] | None = None,
        wall_clock: Callable[[], float] = time.time,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
        cpu_count: Callable[[], int | None] = os.cpu_count,
    ) -> None:
        self.reader = reader or (lambda path: Path(path).read_text(encoding="utf-8"))
        self.wall_clock = wall_clock
        self.loadavg = loadavg
        self.cpu_count = cpu_count

    @staticmethod
    def _parse_cpu(text: str) -> tuple[int, int]:
        first = text.splitlines()[0].split()
        if not first or first[0] != "cpu" or len(first) < 5:
            raise ValueError("/proc/stat does not contain aggregate cpu counters")
        values = [int(value) for value in first[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    @staticmethod
    def _parse_memory(text: str) -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            fields = raw.strip().split()
            if not fields:
                continue
            try:
                amount = int(fields[0])
            except ValueError:
                continue
            values[key] = amount * 1024
        total = values.get("MemTotal")
        if not total:
            raise ValueError("/proc/meminfo is missing MemTotal")
        available = values.get("MemAvailable")
        if available is None:
            available = sum(
                values.get(key, 0) for key in ("MemFree", "Buffers", "Cached", "SReclaimable")
            )
        return total, min(total, max(0, available))

    def read(self) -> HostRawSample:
        cpu_total, cpu_idle = self._parse_cpu(self.reader("/proc/stat"))
        memory_total, memory_available = self._parse_memory(self.reader("/proc/meminfo"))
        uptime_text = self.reader("/proc/uptime").split()
        if not uptime_text:
            raise ValueError("/proc/uptime is empty")
        uptime_s = float(uptime_text[0])
        try:
            load_1, load_5, load_15 = (float(v) for v in self.loadavg())
        except (AttributeError, OSError):
            load_1 = load_5 = load_15 = None
        return HostRawSample(
            timestamp=float(self.wall_clock()),
            cpu_total=cpu_total,
            cpu_idle=cpu_idle,
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
            uptime_s=uptime_s,
            logical_cpus=max(1, int(self.cpu_count() or 1)),
            load_1=load_1,
            load_5=load_5,
            load_15=load_15,
        )


class NativeHostMonitor:
    """Normalize native host counters and preserve unavailable CPU as ``None``."""

    def __init__(self, backend: HostBackend | None = None) -> None:
        if backend is None:
            if os.name == "nt":
                backend = WindowsNativeHostBackend()
            elif sys.platform.startswith("linux"):
                backend = LinuxProcHostBackend()
            else:
                raise OSError(f"unsupported native host platform: {sys.platform}")
        self.backend = backend
        self._previous_cpu: tuple[int, int] | None = None
        self.last_error: str | None = None

    def get_stats(self) -> HostStats | None:
        try:
            raw = self.backend.read()
            cpu_percent: float | None = None
            current = (int(raw.cpu_total), int(raw.cpu_idle))
            if self._previous_cpu is not None:
                previous_total, previous_idle = self._previous_cpu
                total_delta = current[0] - previous_total
                idle_delta = current[1] - previous_idle
                if total_delta > 0 and idle_delta >= 0:
                    busy = max(0, total_delta - min(total_delta, idle_delta))
                    cpu_percent = max(0.0, min(100.0, busy / total_delta * 100.0))
            self._previous_cpu = current

            total = max(0, int(raw.memory_total_bytes))
            available = min(total, max(0, int(raw.memory_available_bytes)))
            used = max(0, total - available)
            percent = used / total * 100.0 if total else 0.0
            self.last_error = None
            return HostStats(
                timestamp=float(raw.timestamp),
                cpu_percent=round(cpu_percent, 2) if cpu_percent is not None else None,
                logical_cpus=max(1, int(raw.logical_cpus)),
                memory_total_mb=round(total / _MIB, 2),
                memory_available_mb=round(available / _MIB, 2),
                memory_used_mb=round(used / _MIB, 2),
                memory_percent=round(percent, 2),
                uptime_s=max(0.0, float(raw.uptime_s)),
                load_1=raw.load_1,
                load_5=raw.load_5,
                load_15=raw.load_15,
            )
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def reset_cpu_baseline(self) -> None:
        self._previous_cpu = None


class HostRuntimeWorker:
    """Continuously collect host checks independent of container selection.

    The queue is count-bounded with drop-oldest behavior. Native API/procfs
    reads happen outside Qt; SQLite persistence remains the consumer's job so a
    desktop connection never crosses threads.
    """

    def __init__(
        self,
        monitor: NativeHostMonitor | None = None,
        *,
        interval_s: float = 1.0,
        max_queue: int = 3600,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if max_queue <= 0:
            raise ValueError("max_queue must be positive")
        self.monitor = monitor or NativeHostMonitor()
        self.interval_s = float(interval_s)
        self.max_queue = int(max_queue)
        self.monotonic_clock = monotonic_clock
        self._items: deque[HostStats] = deque()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._samples_collected = 0
        self._failed_samples = 0
        self._dropped_samples = 0
        self._last_error: str | None = None
        self._last_stats: HostStats | None = None

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.is_running():
                return
            self._stop_requested = False
            self._thread = threading.Thread(
                target=self._run,
                name="native-host-runtime",
                daemon=True,
            )
            self._thread.start()

    def _append(self, stats: HostStats) -> None:
        with self._condition:
            if len(self._items) >= self.max_queue:
                self._items.popleft()
                self._dropped_samples += 1
            self._items.append(stats)
            self._samples_collected += 1
            self._last_stats = stats
            self._last_error = None

    def drain(self, limit: int = 1000) -> tuple[HostStats, ...]:
        if limit <= 0:
            return ()
        result: list[HostStats] = []
        with self._condition:
            while self._items and len(result) < limit:
                result.append(self._items.popleft())
        return tuple(result)

    def requeue_front(self, samples: tuple[HostStats, ...] | list[HostStats]) -> int:
        restored = 0
        with self._condition:
            for sample in reversed(tuple(samples)):
                while len(self._items) >= self.max_queue:
                    self._items.pop()
                    self._dropped_samples += 1
                self._items.appendleft(sample)
                restored += 1
        return restored

    def snapshot(self) -> HostWorkerSnapshot:
        with self._condition:
            return HostWorkerSnapshot(
                running=self.is_running(),
                samples_collected=self._samples_collected,
                failed_samples=self._failed_samples,
                queued_samples=len(self._items),
                dropped_samples=self._dropped_samples,
                last_error=self._last_error,
                last_stats=self._last_stats,
            )

    def stop(self, timeout_s: float = 2.0) -> bool:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout_s))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _run(self) -> None:
        next_due = float(self.monotonic_clock())
        while True:
            with self._condition:
                if self._stop_requested:
                    break

            now = float(self.monotonic_clock())
            if now < next_due:
                with self._condition:
                    if self._stop_requested:
                        break
                    self._condition.wait(min(next_due - now, 0.25))
                continue

            stats = self.monitor.get_stats()
            finished = float(self.monotonic_clock())
            if stats is None:
                with self._condition:
                    self._failed_samples += 1
                    self._last_error = self.monitor.last_error or "native host collection failed"
            else:
                self._append(stats)

            # No catch-up burst after suspend or an unexpectedly slow call.
            next_due = max(next_due + self.interval_s, finished + self.interval_s)
