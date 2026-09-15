"""Native host network and block-I/O counters with reset-safe rate semantics.

Linux is collected directly from procfs/sysfs-style kernel interfaces, without
psutil or another monitoring agent. Raw monotonic counters are kept separate
from derived rates so counter resets, device recreation and suspend gaps never
produce negative or implausible throughput.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Callable, Iterable

_SECTOR_BYTES = 512
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.!+-]{1,128}$")


@dataclass(frozen=True, slots=True)
class NetworkCounters:
    interface: str
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    rx_errors: int
    tx_errors: int
    rx_dropped: int
    tx_dropped: int


@dataclass(frozen=True, slots=True)
class DiskCounters:
    device: str
    reads_completed: int
    writes_completed: int
    read_bytes: int
    write_bytes: int
    io_time_ms: int


@dataclass(frozen=True, slots=True)
class IORate:
    name: str
    read_bps: float | None
    write_bps: float | None
    read_ops_s: float | None
    write_ops_s: float | None
    reset: bool = False


@dataclass(frozen=True, slots=True)
class HostIOSnapshot:
    timestamp: float
    network: tuple[NetworkCounters, ...]
    disks: tuple[DiskCounters, ...]


class LinuxProcIOBackend:
    """Read bounded network and disk counters from Linux procfs."""

    def __init__(
        self,
        *,
        reader: Callable[[str], str] | None = None,
        wall_clock: Callable[[], float] = time.time,
        max_interfaces: int = 256,
        max_devices: int = 256,
    ) -> None:
        if max_interfaces <= 0 or max_devices <= 0:
            raise ValueError("resource bounds must be positive")
        self.reader = reader or (lambda path: Path(path).read_text(encoding="utf-8"))
        self.wall_clock = wall_clock
        self.max_interfaces = int(max_interfaces)
        self.max_devices = int(max_devices)

    @staticmethod
    def parse_net_dev(text: str, *, limit: int = 256) -> tuple[NetworkCounters, ...]:
        result: list[NetworkCounters] = []
        if limit <= 0:
            return ()
        for line in text.splitlines():
            if ":" not in line:
                continue
            raw_name, raw_values = line.split(":", 1)
            name = raw_name.strip()
            fields = raw_values.split()
            if not name or len(fields) < 16 or not _DEVICE_RE.fullmatch(name):
                continue
            try:
                values = [int(value) for value in fields[:16]]
            except ValueError:
                continue
            if any(value < 0 for value in values):
                continue
            result.append(NetworkCounters(
                interface=name,
                rx_bytes=values[0], rx_packets=values[1], rx_errors=values[2], rx_dropped=values[3],
                tx_bytes=values[8], tx_packets=values[9], tx_errors=values[10], tx_dropped=values[11],
            ))
            if len(result) >= limit:
                break
        return tuple(result)

    @staticmethod
    def parse_diskstats(text: str, *, limit: int = 256) -> tuple[DiskCounters, ...]:
        result: list[DiskCounters] = []
        if limit <= 0:
            return ()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 14:
                continue
            name = fields[2]
            if not _DEVICE_RE.fullmatch(name):
                continue
            try:
                reads = int(fields[3]); sectors_read = int(fields[5])
                writes = int(fields[7]); sectors_written = int(fields[9]); io_ms = int(fields[12])
            except ValueError:
                continue
            values = (reads, sectors_read, writes, sectors_written, io_ms)
            if any(value < 0 for value in values):
                continue
            result.append(DiskCounters(
                device=name,
                reads_completed=reads,
                writes_completed=writes,
                read_bytes=sectors_read * _SECTOR_BYTES,
                write_bytes=sectors_written * _SECTOR_BYTES,
                io_time_ms=io_ms,
            ))
            if len(result) >= limit:
                break
        return tuple(result)

    def read(self) -> HostIOSnapshot:
        network = self.parse_net_dev(self.reader("/proc/net/dev"), limit=self.max_interfaces)
        disks = self.parse_diskstats(self.reader("/proc/diskstats"), limit=self.max_devices)
        return HostIOSnapshot(timestamp=float(self.wall_clock()), network=network, disks=disks)


class HostIORateTracker:
    """Derive rates from raw snapshots while detecting reset/recreation.

    The first observation of every interface/device intentionally has unknown
    rates. A backwards counter also yields unknown rates for that interval and
    replaces the baseline. This mirrors agent-style monotonic counter handling.
    """

    def __init__(self, *, max_interval_s: float = 300.0) -> None:
        if max_interval_s <= 0:
            raise ValueError("max_interval_s must be positive")
        self.max_interval_s = float(max_interval_s)
        self._network: dict[str, tuple[float, NetworkCounters]] = {}
        self._disks: dict[str, tuple[float, DiskCounters]] = {}

    @staticmethod
    def _rate(current: int, previous: int, elapsed: float) -> float | None:
        delta = current - previous
        return None if delta < 0 else delta / elapsed

    def update_network(self, timestamp: float, counters: Iterable[NetworkCounters]) -> tuple[IORate, ...]:
        now = float(timestamp)
        current_items = tuple(counters)
        active = {item.interface for item in current_items}
        for stale in set(self._network) - active:
            del self._network[stale]
        result: list[IORate] = []
        for item in current_items:
            previous = self._network.get(item.interface)
            self._network[item.interface] = (now, item)
            if previous is None:
                result.append(IORate(item.interface, None, None, None, None))
                continue
            previous_at, old = previous
            elapsed = now - previous_at
            reset = elapsed <= 0 or elapsed > self.max_interval_s or any((
                item.rx_bytes < old.rx_bytes, item.tx_bytes < old.tx_bytes,
                item.rx_packets < old.rx_packets, item.tx_packets < old.tx_packets,
            ))
            if reset:
                result.append(IORate(item.interface, None, None, None, None, reset=True))
                continue
            result.append(IORate(
                item.interface,
                self._rate(item.rx_bytes, old.rx_bytes, elapsed),
                self._rate(item.tx_bytes, old.tx_bytes, elapsed),
                self._rate(item.rx_packets, old.rx_packets, elapsed),
                self._rate(item.tx_packets, old.tx_packets, elapsed),
            ))
        return tuple(result)

    def update_disks(self, timestamp: float, counters: Iterable[DiskCounters]) -> tuple[IORate, ...]:
        now = float(timestamp)
        current_items = tuple(counters)
        active = {item.device for item in current_items}
        for stale in set(self._disks) - active:
            del self._disks[stale]
        result: list[IORate] = []
        for item in current_items:
            previous = self._disks.get(item.device)
            self._disks[item.device] = (now, item)
            if previous is None:
                result.append(IORate(item.device, None, None, None, None))
                continue
            previous_at, old = previous
            elapsed = now - previous_at
            reset = elapsed <= 0 or elapsed > self.max_interval_s or any((
                item.read_bytes < old.read_bytes, item.write_bytes < old.write_bytes,
                item.reads_completed < old.reads_completed, item.writes_completed < old.writes_completed,
            ))
            if reset:
                result.append(IORate(item.device, None, None, None, None, reset=True))
                continue
            result.append(IORate(
                item.device,
                self._rate(item.read_bytes, old.read_bytes, elapsed),
                self._rate(item.write_bytes, old.write_bytes, elapsed),
                self._rate(item.reads_completed, old.reads_completed, elapsed),
                self._rate(item.writes_completed, old.writes_completed, elapsed),
            ))
        return tuple(result)
