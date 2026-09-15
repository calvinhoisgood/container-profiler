"""Hardware-free unit tests for the profiling core."""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from profiler.core.data_manager import DataManager
from profiler.core.docker_monitor import DockerMonitor
from profiler.core.models import ContainerStats, PowerStats
from profiler.core.power_monitor import HWiNFOReader, NVMLReader, PowerMonitor


class SequenceClock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def __call__(self) -> float:
        if not self.values:
            raise AssertionError("clock exhausted")
        return self.values.pop(0)


class FakeContainer:
    def __init__(self, snapshots: list[dict], *, container_id: str = "abc123") -> None:
        self._snapshots = list(snapshots)
        self.id = container_id
        self.short_id = container_id
        self.name = "trainer"
        self.status = "running"
        self.image = SimpleNamespace(tags=["demo:latest"])
        self.attrs = {"Created": "2026-01-01T00:00:00Z", "Config": {"Image": "demo:latest"}}

    def stats(self, stream: bool = False) -> dict:
        self.last_stream = stream
        return self._snapshots.pop(0)


class FakeContainerCollection:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container

    def get(self, container_id: str) -> FakeContainer:
        if container_id != self.container.id:
            raise KeyError(container_id)
        return self.container

    def list(self, all: bool = True) -> list[FakeContainer]:
        return [self.container]


class FakeClient:
    def __init__(self, container: FakeContainer) -> None:
        self.containers = FakeContainerCollection(container)

    def ping(self) -> bool:
        return True


def snapshot(
    *,
    cpu_total: int = 200,
    pre_cpu_total: int = 100,
    system_total: int = 2000,
    pre_system_total: int = 1000,
    cpus: int = 4,
    memory_usage: int = 512 * 1024 * 1024,
    inactive_file: int = 128 * 1024 * 1024,
    memory_limit: int = 1024 * 1024 * 1024,
    rx: int = 1000,
    tx: int = 2000,
) -> dict:
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu_total},
            "system_cpu_usage": system_total,
            "online_cpus": cpus,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": pre_cpu_total},
            "system_cpu_usage": pre_system_total,
        },
        "memory_stats": {
            "usage": memory_usage,
            "limit": memory_limit,
            "stats": {"inactive_file": inactive_file},
        },
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
        "pids_stats": {"current": 7},
    }


class DockerMonitorTests(unittest.TestCase):
    def test_list_containers_uses_real_metadata(self) -> None:
        container = FakeContainer([snapshot()])
        monitor = DockerMonitor(client=FakeClient(container))
        items = monitor.list_containers()
        self.assertEqual(items[0].name, "trainer")
        self.assertEqual(items[0].image, "demo:latest")

    def test_cpu_memory_and_first_network_sample(self) -> None:
        container = FakeContainer([snapshot()])
        monitor = DockerMonitor(
            client=FakeClient(container),
            monotonic_clock=SequenceClock(10.0),
            wall_clock=SequenceClock(1000.0),
        )
        stats = monitor.get_stats("abc123")
        assert stats is not None
        self.assertAlmostEqual(stats.cpu_percent, 40.0)
        self.assertAlmostEqual(stats.memory_mb, 384.0)
        self.assertAlmostEqual(stats.memory_percent, 37.5)
        self.assertIsNone(stats.network_rx_bps)
        self.assertEqual(stats.pids, 7)

    def test_network_rate_is_delta_over_monotonic_time(self) -> None:
        container = FakeContainer([snapshot(rx=1000, tx=2000), snapshot(rx=5000, tx=5000)])
        monitor = DockerMonitor(
            client=FakeClient(container),
            monotonic_clock=SequenceClock(10.0, 12.0),
            wall_clock=SequenceClock(1000.0, 1002.0),
        )
        monitor.get_stats("abc123")
        second = monitor.get_stats("abc123")
        assert second is not None
        self.assertEqual(second.network_rx_bps, 2000.0)
        self.assertEqual(second.network_tx_bps, 1500.0)

    def test_network_counter_reset_never_becomes_negative(self) -> None:
        container = FakeContainer([snapshot(rx=5000, tx=5000), snapshot(rx=10, tx=20)])
        monitor = DockerMonitor(
            client=FakeClient(container),
            monotonic_clock=SequenceClock(1.0, 2.0),
            wall_clock=SequenceClock(1.0, 2.0),
        )
        monitor.get_stats("abc123")
        second = monitor.get_stats("abc123")
        assert second is not None
        self.assertEqual(second.network_rx_bps, 0.0)
        self.assertEqual(second.network_tx_bps, 0.0)


class SyntheticHWiNFOReader(HWiNFOReader):
    def __init__(self, blob: bytes) -> None:
        self._os_name = "nt"
        self._mapping = None
        self._view = 1
        self._kernel32 = None
        self.last_error = None
        self._blob = blob

    def _read_bytes(self, offset: int, size: int) -> bytes:
        return self._blob[offset : offset + size]

    def close(self) -> None:
        self._view = None


class HWiNFOParserTests(unittest.TestCase):
    def test_parser_accepts_larger_future_reading_stride(self) -> None:
        header_size = HWiNFOReader._HEADER.size
        reading_size = HWiNFOReader._READING.size + 16
        header = HWiNFOReader._HEADER.pack(
            0x53695748, 2, 0, 123, 44, 0, 0, header_size, reading_size, 1
        )
        reading = HWiNFOReader._READING.pack(
            0,
            0,
            1,
            b"CPU Package Power".ljust(128, b"\x00"),
            b"".ljust(128, b"\x00"),
            b"W".ljust(16, b"\x00"),
            88.25,
            0.0,
            0.0,
            0.0,
        )
        reader = SyntheticHWiNFOReader(header + reading + (b"\x00" * 16))
        self.assertEqual(reader.get_cpu_power(), 88.25)


class FakeNVML:
    NVML_TEMPERATURE_GPU = 0

    def __init__(self) -> None:
        self.shutdown = False

    def nvmlInit(self) -> None: pass
    def nvmlShutdown(self) -> None: self.shutdown = True
    def nvmlDeviceGetCount(self) -> int: return 1
    def nvmlDeviceGetHandleByIndex(self, index: int) -> int: return index
    def nvmlDeviceGetPowerUsage(self, handle: int) -> int: return 123_000
    def nvmlDeviceGetUtilizationRates(self, handle: int): return SimpleNamespace(gpu=77)
    def nvmlDeviceGetMemoryInfo(self, handle: int): return SimpleNamespace(used=2 * 1024**3, total=8 * 1024**3)
    def nvmlDeviceGetTemperature(self, handle: int, sensor: int) -> int: return 66


class FakeHWiNFO:
    def is_connected(self) -> bool: return True
    def get_cpu_power(self) -> float: return 42.5
    def close(self) -> None: pass


class PowerMonitorTests(unittest.TestCase):
    def test_hwinfo_reader_is_import_safe_off_windows(self) -> None:
        reader = HWiNFOReader(os_name="posix")
        self.assertFalse(reader.is_connected())
        self.assertIn("Windows", reader.last_error or "")

    def test_nvml_reader_and_facade(self) -> None:
        module = FakeNVML()
        monitor = PowerMonitor(hwinfo=FakeHWiNFO(), nvml=NVMLReader(module))
        stats = monitor.get_power_stats()
        self.assertEqual(stats.cpu_power_w, 42.5)
        self.assertEqual(stats.gpu_power_w, 123.0)
        self.assertEqual(stats.gpu_memory_total_mb, 8192.0)
        monitor.close()
        self.assertTrue(module.shutdown)


class DataManagerTests(unittest.TestCase):
    def test_csv_keeps_missing_sensor_values_blank(self) -> None:
        manager = DataManager()
        manager.start_recording()
        stats = ContainerStats(
            timestamp=1_700_000_000.0,
            cpu_percent=12.5,
            memory_mb=100.0,
            memory_limit_mb=200.0,
            memory_percent=50.0,
            network_rx_bytes=10,
            network_tx_bytes=20,
            network_rx_bps=None,
            network_tx_bps=0.0,
            pids=3,
        )
        manager.add_record("abc123", stats, PowerStats(cpu_power_w=None, gpu_power_w=0.0))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.csv"
            self.assertTrue(manager.export_csv(path))
            row = next(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))

        self.assertEqual(row["network_rx_bps"], "")
        self.assertEqual(row["network_tx_bps"], "0.0")
        self.assertEqual(row["cpu_power_w"], "")
        self.assertEqual(row["gpu_power_w"], "0.0")


if __name__ == "__main__":
    unittest.main()
