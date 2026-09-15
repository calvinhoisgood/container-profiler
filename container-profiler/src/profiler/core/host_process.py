"""Dependency-free native host process summary collection.

The collector deliberately emits a bounded, low-cardinality host summary rather
than process names, command lines, or per-PID metric series. Linux reads procfs;
Windows uses the documented Tool Help process snapshot API. Optional richer
process inventory can be layered on top without making the core agent depend on
psutil, WMI providers, or another monitoring product.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Protocol


@dataclass(frozen=True, slots=True)
class ProcessSummary:
    timestamp: float
    processes: int
    threads: int
    running: int | None = None
    sleeping: int | None = None
    blocked: int | None = None
    stopped: int | None = None
    zombies: int | None = None
    unknown: int | None = None
    skipped_processes: int = 0


class ProcessBackend(Protocol):
    def read(self) -> ProcessSummary: ...


class LinuxProcProcessBackend:
    """Summarize Linux processes from ``/proc/<pid>/stat`` without psutil."""

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        wall_clock: Callable[[], float] = time.time,
        listdir: Callable[[str | Path], Iterable[str]] = os.listdir,
        reader: Callable[[str | Path], str] | None = None,
    ) -> None:
        self.proc_root = Path(proc_root)
        self.wall_clock = wall_clock
        self.listdir = listdir
        self.reader = reader or (
            lambda path: Path(path).read_text(encoding="utf-8", errors="replace")
        )

    @staticmethod
    def _parse_stat(text: str) -> tuple[str, int]:
        # proc(5) allows spaces and ')' in comm. The state field starts after
        # the final ") " delimiter, so splitting the whole line is unsafe.
        line = text.strip()
        delimiter = line.rfind(") ")
        if delimiter < 0:
            raise ValueError("process stat is missing command delimiter")
        fields = line[delimiter + 2 :].split()
        # fields[0] is state (field 3) and fields[17] is num_threads (field 20).
        if len(fields) < 18:
            raise ValueError("process stat is missing required fields")
        state = fields[0]
        if len(state) != 1:
            raise ValueError("process stat has invalid state")
        threads = int(fields[17])
        if threads < 0:
            raise ValueError("process stat has negative thread count")
        return state, threads

    def read(self) -> ProcessSummary:
        processes = 0
        threads = 0
        running = sleeping = blocked = stopped = zombies = unknown = 0
        skipped = 0

        for raw_pid in self.listdir(self.proc_root):
            pid = str(raw_pid)
            if not pid.isdigit():
                continue
            try:
                state, thread_count = self._parse_stat(
                    self.reader(self.proc_root / pid / "stat")
                )
            except (OSError, UnicodeError, ValueError):
                # Processes can exit between readdir and reading stat. Treat the
                # snapshot as best effort and surface the skipped count.
                skipped += 1
                continue

            processes += 1
            threads += thread_count
            if state == "R":
                running += 1
            elif state in {"S", "I"}:
                sleeping += 1
            elif state == "D":
                blocked += 1
            elif state in {"T", "t"}:
                stopped += 1
            elif state == "Z":
                zombies += 1
            else:
                unknown += 1

        return ProcessSummary(
            timestamp=float(self.wall_clock()),
            processes=processes,
            threads=threads,
            running=running,
            sleeping=sleeping,
            blocked=blocked,
            stopped=stopped,
            zombies=zombies,
            unknown=unknown,
            skipped_processes=skipped,
        )


_MAX_PATH = 260
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


class WindowsToolhelpProcessBackend:
    """Summarize Windows processes through Kernel32 Tool Help snapshots."""

    def __init__(
        self,
        *,
        kernel32: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("Windows process backend is only available on Windows")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
            kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            kernel32.Process32FirstW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32FirstW.restype = ctypes.c_int
            kernel32.Process32NextW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
        self.kernel32 = kernel32
        self.wall_clock = wall_clock

    @staticmethod
    def _summarize_entries(
        entries: Iterable[tuple[int, int]], *, timestamp: float
    ) -> ProcessSummary:
        rows = tuple(entries)
        return ProcessSummary(
            timestamp=float(timestamp),
            processes=len(rows),
            threads=sum(max(0, int(thread_count)) for _, thread_count in rows),
        )

    @staticmethod
    def _handle_value(handle: Any) -> int | None:
        if handle is None:
            return None
        value = getattr(handle, "value", handle)
        return None if value is None else int(value)

    def read(self) -> ProcessSummary:
        handle = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        value = self._handle_value(handle)
        if value in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

        entries: list[tuple[int, int]] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not self.kernel32.Process32FirstW(handle, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == _ERROR_NO_MORE_FILES:
                    return self._summarize_entries((), timestamp=self.wall_clock())
                raise OSError(error, "Process32FirstW failed")

            while True:
                entries.append((int(entry.th32ProcessID), int(entry.cntThreads)))
                ctypes.set_last_error(0)
                if not self.kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error not in (0, _ERROR_NO_MORE_FILES):
                        raise OSError(error, "Process32NextW failed")
                    break
        finally:
            self.kernel32.CloseHandle(handle)

        return self._summarize_entries(entries, timestamp=self.wall_clock())


def create_native_process_backend() -> ProcessBackend:
    if os.name == "nt":
        return WindowsToolhelpProcessBackend()
    if sys.platform.startswith("linux"):
        return LinuxProcProcessBackend()
    raise OSError(f"unsupported native process platform: {sys.platform}")


class NativeProcessMonitor:
    """Failure-isolating facade used by the long-running system collector."""

    def __init__(self, backend: ProcessBackend | None = None) -> None:
        self.backend = backend or create_native_process_backend()
        self.last_error: str | None = None

    def get_stats(self) -> ProcessSummary | None:
        try:
            stats = self.backend.read()
        except Exception as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return stats
