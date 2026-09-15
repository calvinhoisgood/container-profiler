import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.host_monitor import (
    HostRawSample,
    HostRuntimeWorker,
    HostStats,
    LinuxProcHostBackend,
    NativeHostMonitor,
    WindowsNativeHostBackend,
)


class SequenceBackend:
    def __init__(self, *samples):
        self.samples = list(samples)

    def read(self):
        value = self.samples.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class NativeHostMonitorTests(unittest.TestCase):
    def raw(self, total, idle, *, available=4 * 1024**3):
        return HostRawSample(
            timestamp=float(total),
            cpu_total=total,
            cpu_idle=idle,
            memory_total_bytes=8 * 1024**3,
            memory_available_bytes=available,
            uptime_s=123.0,
            logical_cpus=8,
            load_1=1.0,
            load_5=2.0,
            load_15=3.0,
        )

    def test_first_cpu_sample_is_unknown_then_delta_is_computed(self):
        monitor = NativeHostMonitor(SequenceBackend(
            self.raw(1000, 500),
            self.raw(1200, 550),
        ))
        first = monitor.get_stats()
        second = monitor.get_stats()
        self.assertIsNone(first.cpu_percent)
        self.assertEqual(second.cpu_percent, 75.0)
        self.assertEqual(second.memory_total_mb, 8192.0)
        self.assertEqual(second.memory_used_mb, 4096.0)
        self.assertEqual(second.memory_percent, 50.0)
        self.assertEqual(second.logical_cpus, 8)
        self.assertEqual(second.load_15, 3.0)

    def test_counter_reset_does_not_emit_bogus_cpu_percent(self):
        monitor = NativeHostMonitor(SequenceBackend(
            self.raw(1000, 500),
            self.raw(10, 5),
        ))
        monitor.get_stats()
        self.assertIsNone(monitor.get_stats().cpu_percent)

    def test_backend_failure_degrades_without_raising(self):
        monitor = NativeHostMonitor(SequenceBackend(RuntimeError("host API down")))
        self.assertIsNone(monitor.get_stats())
        self.assertEqual(monitor.last_error, "host API down")


class LinuxProcBackendTests(unittest.TestCase):
    def test_procfs_parsing_and_injected_read(self):
        files = {
            "/proc/stat": "cpu  100 20 30 400 50 6 7 8 0 0\ncpu0 1 2 3 4\n",
            "/proc/meminfo": (
                "MemTotal:       1000000 kB\n"
                "MemAvailable:    250000 kB\n"
                "MemFree:         100000 kB\n"
            ),
            "/proc/uptime": "12345.50 1000.0\n",
        }
        backend = LinuxProcHostBackend(
            reader=lambda path: files[path],
            wall_clock=lambda: 99.0,
            loadavg=lambda: (0.5, 1.0, 1.5),
            cpu_count=lambda: 4,
        )
        sample = backend.read()
        self.assertEqual(sample.cpu_total, 621)
        self.assertEqual(sample.cpu_idle, 450)
        self.assertEqual(sample.memory_total_bytes, 1_000_000 * 1024)
        self.assertEqual(sample.memory_available_bytes, 250_000 * 1024)
        self.assertEqual(sample.uptime_s, 12345.5)
        self.assertEqual(sample.logical_cpus, 4)
        self.assertEqual(sample.load_5, 1.0)

    def test_memavailable_fallback_is_bounded_by_total(self):
        total, available = LinuxProcHostBackend._parse_memory(
            "MemTotal: 100 kB\nMemFree: 50 kB\nBuffers: 30 kB\n"
            "Cached: 40 kB\nSReclaimable: 20 kB\n"
        )
        self.assertEqual(total, 100 * 1024)
        self.assertEqual(available, total)

    def test_bad_proc_cpu_is_rejected(self):
        with self.assertRaises(ValueError):
            LinuxProcHostBackend._parse_cpu("intr 1 2 3\n")


class FakeKernel32:
    def GetSystemTimes(self, idle, kernel, user):
        idle._obj.dwLowDateTime = 100
        idle._obj.dwHighDateTime = 0
        kernel._obj.dwLowDateTime = 700
        kernel._obj.dwHighDateTime = 0
        user._obj.dwLowDateTime = 300
        user._obj.dwHighDateTime = 0
        return 1

    def GlobalMemoryStatusEx(self, memory):
        memory._obj.ullTotalPhys = 16 * 1024**3
        memory._obj.ullAvailPhys = 6 * 1024**3
        return 1

    def GetTickCount64(self):
        return 12_345


class WindowsNativeBackendTests(unittest.TestCase):
    def test_kernel32_adapter_without_windows_or_third_party_library(self):
        backend = WindowsNativeHostBackend(
            kernel32=FakeKernel32(),
            wall_clock=lambda: 42.0,
            cpu_count=lambda: 16,
        )
        sample = backend.read()
        # Kernel time includes idle according to the Windows API contract.
        self.assertEqual(sample.cpu_total, 1000)
        self.assertEqual(sample.cpu_idle, 100)
        self.assertEqual(sample.memory_total_bytes, 16 * 1024**3)
        self.assertEqual(sample.memory_available_bytes, 6 * 1024**3)
        self.assertEqual(sample.uptime_s, 12.345)
        self.assertEqual(sample.logical_cpus, 16)


class RepeatingMonitor:
    def __init__(self):
        self.calls = 0
        self.last_error = None

    def get_stats(self):
        self.calls += 1
        return HostStats(
            timestamp=float(self.calls),
            cpu_percent=25.0,
            logical_cpus=4,
            memory_total_mb=8192.0,
            memory_available_mb=4096.0,
            memory_used_mb=4096.0,
            memory_percent=50.0,
            uptime_s=100.0 + self.calls,
        )


class HostRuntimeWorkerTests(unittest.TestCase):
    def test_worker_runs_without_container_selection_and_queue_is_bounded(self):
        monitor = RepeatingMonitor()
        worker = HostRuntimeWorker(monitor, interval_s=0.01, max_queue=2)
        worker.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and worker.snapshot().samples_collected < 3:
            time.sleep(0.01)
        snapshot = worker.snapshot()
        self.assertGreaterEqual(snapshot.samples_collected, 3)
        self.assertLessEqual(snapshot.queued_samples, 2)
        self.assertGreaterEqual(snapshot.dropped_samples, 1)
        drained = worker.drain(2)
        self.assertGreaterEqual(len(drained), 1)
        self.assertEqual(worker.requeue_front(drained), len(drained))
        self.assertTrue(worker.stop(timeout_s=1.0))
        self.assertFalse(worker.is_running())


if __name__ == "__main__":
    unittest.main()
