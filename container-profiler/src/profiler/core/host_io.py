"""Native host network and block-I/O counters with reset-safe rate semantics.

Linux reads procfs directly. Windows uses documented IP Helper and disk IOCTL
interfaces. No psutil or third-party monitoring application is required. Raw
monotonic counters stay separate from derived rates so counter resets, device
recreation and suspend gaps never produce negative or implausible throughput.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

_SECTOR_BYTES = 512
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.!+-]{1,128}$")
_IF_MAX_STRING_SIZE = 256
_IF_MAX_PHYS_ADDRESS_LENGTH = 32
_IOCTL_DISK_PERFORMANCE = 0x00070020
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


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
    network_error: str | None = None
    disk_error: str | None = None


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
        network_error: str | None = None
        disk_error: str | None = None
        try:
            network = self.parse_net_dev(self.reader("/proc/net/dev"), limit=self.max_interfaces)
        except Exception as exc:
            network = ()
            network_error = str(exc)
        try:
            disks = self.parse_diskstats(self.reader("/proc/diskstats"), limit=self.max_devices)
        except Exception as exc:
            disks = ()
            disk_error = str(exc)
        return HostIOSnapshot(
            timestamp=float(self.wall_clock()),
            network=network,
            disks=disks,
            network_error=network_error,
            disk_error=disk_error,
        )


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _MIB_IF_ROW2(ctypes.Structure):
    # Layout from Microsoft's netioapi.h MIB_IF_ROW2 definition. The structure
    # is only dereferenced on Windows; fixed-width integer types keep the ABI
    # independent of Python's host C long size.
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("InterfaceGuid", _GUID),
        ("Alias", ctypes.c_wchar * (_IF_MAX_STRING_SIZE + 1)),
        ("Description", ctypes.c_wchar * (_IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("PhysicalAddress", ctypes.c_ubyte * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TunnelType", ctypes.c_uint32),
        ("MediaType", ctypes.c_uint32),
        ("PhysicalMediumType", ctypes.c_uint32),
        ("AccessType", ctypes.c_uint32),
        ("DirectionType", ctypes.c_uint32),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_uint32),
        ("AdminStatus", ctypes.c_uint32),
        ("MediaConnectState", ctypes.c_uint32),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_uint32),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


class _MIB_IF_TABLE2_HEADER(ctypes.Structure):
    _fields_ = [
        ("NumEntries", ctypes.c_uint32),
        ("Table", _MIB_IF_ROW2 * 1),
    ]


class _DISK_PERFORMANCE(ctypes.Structure):
    _fields_ = [
        ("BytesRead", ctypes.c_int64),
        ("BytesWritten", ctypes.c_int64),
        ("ReadTime", ctypes.c_int64),
        ("WriteTime", ctypes.c_int64),
        ("IdleTime", ctypes.c_int64),
        ("ReadCount", ctypes.c_uint32),
        ("WriteCount", ctypes.c_uint32),
        ("QueueDepth", ctypes.c_uint32),
        ("SplitCount", ctypes.c_uint32),
        ("QueryTime", ctypes.c_int64),
        ("StorageDeviceNumber", ctypes.c_uint32),
        ("StorageManagerName", ctypes.c_wchar * 8),
    ]


class _WindowsIPHelper:
    def __init__(self, dll: Any | None = None) -> None:
        if dll is None:
            if os.name != "nt":
                raise OSError("Windows IP Helper is only available on Windows")
            dll = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.dll = dll
        self.dll.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.dll.GetIfTable2.restype = ctypes.c_uint32
        self.dll.FreeMibTable.argtypes = [ctypes.c_void_p]
        self.dll.FreeMibTable.restype = None

    def read(self, limit: int) -> tuple[NetworkCounters, ...]:
        table_ptr = ctypes.c_void_p()
        status = int(self.dll.GetIfTable2(ctypes.byref(table_ptr)))
        if status != 0:
            raise OSError(status, "GetIfTable2 failed")
        if not table_ptr.value:
            raise OSError("GetIfTable2 returned a null table")
        result: list[NetworkCounters] = []
        try:
            header = ctypes.cast(table_ptr, ctypes.POINTER(_MIB_IF_TABLE2_HEADER)).contents
            count = min(int(header.NumEntries), max(0, int(limit)))
            row_base = int(table_ptr.value) + _MIB_IF_TABLE2_HEADER.Table.offset
            row_size = ctypes.sizeof(_MIB_IF_ROW2)
            for index in range(count):
                row = ctypes.cast(
                    row_base + index * row_size,
                    ctypes.POINTER(_MIB_IF_ROW2),
                ).contents
                alias = str(row.Alias).split("\x00", 1)[0].strip()
                description = str(row.Description).split("\x00", 1)[0].strip()
                name = alias or description or f"ifindex-{int(row.InterfaceIndex)}"
                name = name[:256]
                result.append(NetworkCounters(
                    interface=name,
                    rx_bytes=int(row.InOctets),
                    tx_bytes=int(row.OutOctets),
                    rx_packets=int(row.InUcastPkts + row.InNUcastPkts),
                    tx_packets=int(row.OutUcastPkts + row.OutNUcastPkts),
                    rx_errors=int(row.InErrors),
                    tx_errors=int(row.OutErrors),
                    rx_dropped=int(row.InDiscards),
                    tx_dropped=int(row.OutDiscards),
                ))
        finally:
            self.dll.FreeMibTable(table_ptr)
        return tuple(result)


class _WindowsDiskIO:
    def __init__(self, kernel32: Any | None = None) -> None:
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("Windows disk I/O is only available on Windows")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32 = kernel32
        self.kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        self.kernel32.CreateFileW.restype = ctypes.c_void_p
        self.kernel32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        self.kernel32.DeviceIoControl.restype = ctypes.c_int
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int

    def read(self, limit: int) -> tuple[DiskCounters, ...]:
        result: list[DiskCounters] = []
        for index in range(max(0, int(limit))):
            path = rf"\\.\PhysicalDrive{index}"
            handle = self.kernel32.CreateFileW(
                path, 0, _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                _OPEN_EXISTING, 0, None,
            )
            handle_value = int(handle) if handle is not None else 0
            if not handle_value or handle_value == _INVALID_HANDLE_VALUE:
                continue
            try:
                performance = _DISK_PERFORMANCE()
                returned = ctypes.c_uint32()
                ok = self.kernel32.DeviceIoControl(
                    handle, _IOCTL_DISK_PERFORMANCE, None, 0,
                    ctypes.byref(performance), ctypes.sizeof(performance),
                    ctypes.byref(returned), None,
                )
                if not ok or returned.value < ctypes.sizeof(_DISK_PERFORMANCE):
                    continue
                if performance.BytesRead < 0 or performance.BytesWritten < 0:
                    continue
                busy_100ns = max(0, int(performance.QueryTime) - int(performance.IdleTime))
                result.append(DiskCounters(
                    device=f"PhysicalDrive{index}",
                    reads_completed=int(performance.ReadCount),
                    writes_completed=int(performance.WriteCount),
                    read_bytes=int(performance.BytesRead),
                    write_bytes=int(performance.BytesWritten),
                    io_time_ms=busy_100ns // 10_000,
                ))
            finally:
                self.kernel32.CloseHandle(handle)
        return tuple(result)


class WindowsNativeIOBackend:
    """Read Windows network/disk counters from documented native APIs.

    Readers are injectable so parsing/bounds/degradation semantics remain fully
    hardware-free testable. By default the backend uses GetIfTable2 and
    IOCTL_DISK_PERFORMANCE directly.
    """

    def __init__(
        self,
        *,
        network_reader: Callable[[int], Iterable[NetworkCounters]] | None = None,
        disk_reader: Callable[[int], Iterable[DiskCounters]] | None = None,
        wall_clock: Callable[[], float] = time.time,
        max_interfaces: int = 256,
        max_devices: int = 64,
        iphlpapi: Any | None = None,
        kernel32: Any | None = None,
    ) -> None:
        if max_interfaces <= 0 or max_devices <= 0:
            raise ValueError("resource bounds must be positive")
        self.max_interfaces = int(max_interfaces)
        self.max_devices = int(max_devices)
        self.wall_clock = wall_clock
        if network_reader is None:
            network_reader = _WindowsIPHelper(iphlpapi).read
        if disk_reader is None:
            disk_reader = _WindowsDiskIO(kernel32).read
        self.network_reader = network_reader
        self.disk_reader = disk_reader

    @staticmethod
    def _bounded(items: Iterable[Any], limit: int) -> tuple[Any, ...]:
        result = []
        for item in items:
            result.append(item)
            if len(result) >= limit:
                break
        return tuple(result)

    def read(self) -> HostIOSnapshot:
        network_error: str | None = None
        disk_error: str | None = None
        try:
            network = self._bounded(self.network_reader(self.max_interfaces), self.max_interfaces)
        except Exception as exc:
            network = ()
            network_error = str(exc)
        try:
            disks = self._bounded(self.disk_reader(self.max_devices), self.max_devices)
        except Exception as exc:
            disks = ()
            disk_error = str(exc)
        return HostIOSnapshot(
            timestamp=float(self.wall_clock()),
            network=tuple(network),
            disks=tuple(disks),
            network_error=network_error,
            disk_error=disk_error,
        )


def create_native_io_backend(**kwargs) -> LinuxProcIOBackend | WindowsNativeIOBackend:
    if os.name == "nt":
        return WindowsNativeIOBackend(**kwargs)
    if os.name == "posix":
        return LinuxProcIOBackend(**kwargs)
    raise OSError(f"unsupported native I/O platform: {os.name}")


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
