"""Dependency-free host filesystem capacity collection.

The collector deliberately uses OS primitives instead of a third-party monitoring
application. Linux mount discovery is procfs-backed; Windows volume capacity is
queried through Kernel32. Results are bounded and normalized for persistence or
forwarding by higher layers.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable


@dataclass(frozen=True, slots=True)
class FilesystemStats:
    timestamp: float
    mountpoint: str
    device: str | None
    filesystem: str | None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float


def _decode_mount_field(value: str) -> str:
    # proc mount fields escape whitespace/backslash as octal sequences.
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


class LinuxFilesystemBackend:
    """Read mounted local filesystems using /proc/self/mounts + statvfs."""

    DEFAULT_IGNORED_TYPES = frozenset({
        "autofs", "binfmt_misc", "cgroup", "cgroup2", "configfs", "debugfs",
        "devpts", "devtmpfs", "fusectl", "hugetlbfs", "mqueue", "proc",
        "pstore", "securityfs", "sysfs", "tracefs",
    })

    def __init__(
        self,
        *,
        mounts_reader: Callable[[], str] | None = None,
        statvfs: Callable[[str], Any] = os.statvfs,
        wall_clock: Callable[[], float] = time.time,
        ignored_types: Iterable[str] | None = None,
        max_filesystems: int = 256,
    ) -> None:
        if max_filesystems <= 0:
            raise ValueError("max_filesystems must be positive")
        self.mounts_reader = mounts_reader or (
            lambda: Path("/proc/self/mounts").read_text(encoding="utf-8")
        )
        self.statvfs = statvfs
        self.wall_clock = wall_clock
        self.ignored_types = frozenset(ignored_types or self.DEFAULT_IGNORED_TYPES)
        self.max_filesystems = int(max_filesystems)

    def read(self) -> tuple[FilesystemStats, ...]:
        now = float(self.wall_clock())
        results: list[FilesystemStats] = []
        seen: set[str] = set()
        for line in self.mounts_reader().splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            device, mountpoint, fs_type = map(_decode_mount_field, fields[:3])
            if fs_type in self.ignored_types or mountpoint in seen:
                continue
            seen.add(mountpoint)
            try:
                info = self.statvfs(mountpoint)
            except OSError:
                continue
            block_size = int(info.f_frsize or info.f_bsize)
            total = max(0, int(info.f_blocks) * block_size)
            # bavail is the space usable by an unprivileged Agent process.
            free = min(total, max(0, int(info.f_bavail) * block_size))
            used = max(0, total - free)
            percent = used / total * 100.0 if total else 0.0
            results.append(FilesystemStats(
                timestamp=now,
                mountpoint=mountpoint,
                device=device,
                filesystem=fs_type,
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                used_percent=round(percent, 2),
            ))
            if len(results) >= self.max_filesystems:
                break
        return tuple(results)


class WindowsFilesystemBackend:
    """Read drive capacity directly from documented Kernel32 APIs."""

    def __init__(
        self,
        *,
        kernel32: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
        max_filesystems: int = 64,
    ) -> None:
        if max_filesystems <= 0:
            raise ValueError("max_filesystems must be positive")
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("Windows filesystem backend is only available on Windows")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetLogicalDrives.argtypes = []
            kernel32.GetLogicalDrives.restype = ctypes.c_uint32
            kernel32.GetDiskFreeSpaceExW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
            kernel32.GetDiskFreeSpaceExW.restype = ctypes.c_int
        self.kernel32 = kernel32
        self.wall_clock = wall_clock
        self.max_filesystems = int(max_filesystems)

    def read(self) -> tuple[FilesystemStats, ...]:
        mask = int(self.kernel32.GetLogicalDrives())
        now = float(self.wall_clock())
        results: list[FilesystemStats] = []
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            available = ctypes.c_uint64()
            total = ctypes.c_uint64()
            free_total = ctypes.c_uint64()
            if not self.kernel32.GetDiskFreeSpaceExW(
                root, ctypes.byref(available), ctypes.byref(total), ctypes.byref(free_total)
            ):
                # Removable/offline drives must not break the whole host check.
                continue
            total_value = int(total.value)
            free_value = min(total_value, int(available.value))
            used = max(0, total_value - free_value)
            percent = used / total_value * 100.0 if total_value else 0.0
            results.append(FilesystemStats(
                timestamp=now,
                mountpoint=root,
                device=root,
                filesystem=None,
                total_bytes=total_value,
                used_bytes=used,
                free_bytes=free_value,
                used_percent=round(percent, 2),
            ))
            if len(results) >= self.max_filesystems:
                break
        return tuple(results)


class NativeFilesystemMonitor:
    def __init__(self, backend: Any | None = None) -> None:
        if backend is None:
            if os.name == "nt":
                backend = WindowsFilesystemBackend()
            elif sys.platform.startswith("linux"):
                backend = LinuxFilesystemBackend()
            else:
                raise OSError(f"unsupported filesystem platform: {sys.platform}")
        self.backend = backend
        self.last_error: str | None = None

    def get_stats(self) -> tuple[FilesystemStats, ...]:
        try:
            stats = tuple(self.backend.read())
            self.last_error = None
            return stats
        except Exception as exc:
            self.last_error = str(exc)
            return ()
