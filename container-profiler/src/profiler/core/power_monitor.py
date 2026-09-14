"""Optional host power sensors: HWiNFO shared memory and NVIDIA NVML."""
from __future__ import annotations

import ctypes
import os
import struct
from typing import Any, Optional

from .models import PowerStats


class HWiNFOReader:
    """Read CPU Package Power from HWiNFO's Windows shared memory.

    The class is intentionally import-safe on non-Windows platforms. That makes
    unit testing and documentation builds possible without hiding platform bugs.
    """

    SHARED_MEMORY_NAME = r"Global\HWiNFO_SENS_SM2"
    _HEADER = struct.Struct("<IIIQIIIIII")
    _READING = struct.Struct("<III128s128s16sdddd")

    def __init__(self, *, os_name: str | None = None) -> None:
        self._os_name = os_name or os.name
        self._mapping: Any | None = None
        self._view: int | None = None
        self._kernel32: Any | None = None
        self.last_error: str | None = None
        if self._os_name == "nt":
            self._connect()
        else:
            self.last_error = "HWiNFO shared memory is only available on Windows"

    def _connect(self) -> None:
        try:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_mapping = kernel32.OpenFileMappingW
            open_mapping.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            open_mapping.restype = wintypes.HANDLE

            map_view = kernel32.MapViewOfFile
            map_view.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_size_t,
            ]
            map_view.restype = ctypes.c_void_p

            mapping = open_mapping(0x0004, False, self.SHARED_MEMORY_NAME)
            if not mapping:
                self.last_error = f"OpenFileMappingW failed ({ctypes.get_last_error()})"
                return

            view = map_view(mapping, 0x0004, 0, 0, 0)
            if not view:
                kernel32.CloseHandle(mapping)
                self.last_error = f"MapViewOfFile failed ({ctypes.get_last_error()})"
                return

            self._kernel32 = kernel32
            self._mapping = mapping
            self._view = int(view)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            self.close()

    def is_connected(self) -> bool:
        return self._view is not None

    def _read_bytes(self, offset: int, size: int) -> bytes:
        if self._view is None:
            raise RuntimeError("HWiNFO shared memory is not connected")
        if offset < 0 or size < 0:
            raise ValueError("negative shared-memory offset/size")
        return ctypes.string_at(self._view + offset, size)

    @staticmethod
    def _decode_label(raw: bytes) -> str:
        return raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore").strip()

    def get_cpu_power(self) -> Optional[float]:
        if self._view is None:
            return None

        try:
            header = self._HEADER.unpack(self._read_bytes(0, self._HEADER.size))
            reading_offset = int(header[7])
            reading_size = int(header[8])
            reading_count = int(header[9])

            if reading_offset <= 0 or reading_count < 0:
                raise ValueError("invalid HWiNFO reading section")
            if reading_size < self._READING.size:
                raise ValueError(
                    f"unsupported HWiNFO reading size: {reading_size} < {self._READING.size}"
                )
            if reading_count > 100_000:
                raise ValueError(f"unreasonable HWiNFO reading count: {reading_count}")

            target = "cpu package power"
            for index in range(reading_count):
                offset = reading_offset + index * reading_size
                values = self._READING.unpack(
                    self._read_bytes(offset, self._READING.size)
                )
                original = self._decode_label(values[3]).lower()
                user = self._decode_label(values[4]).lower()
                if target in original or target in user:
                    value = float(values[6])
                    return value if value >= 0 else None

            return None
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def close(self) -> None:
        if self._kernel32 is not None and self._view is not None:
            try:
                self._kernel32.UnmapViewOfFile(ctypes.c_void_p(self._view))
            except Exception:
                pass
        if self._kernel32 is not None and self._mapping is not None:
            try:
                self._kernel32.CloseHandle(self._mapping)
            except Exception:
                pass
        self._view = None
        self._mapping = None


class NVMLReader:
    """NVIDIA GPU metrics with injectable NVML module for deterministic tests."""

    def __init__(self, module: Any | None = None) -> None:
        self.pynvml: Any | None = None
        self.device_count = 0
        self.initialized = False
        self.last_error: str | None = None
        self._init(module)

    def _init(self, module: Any | None) -> None:
        try:
            if module is None:
                import pynvml as module
            module.nvmlInit()
            self.pynvml = module
            self.device_count = int(module.nvmlDeviceGetCount())
            self.initialized = True
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            self.initialized = False
            self.device_count = 0

    def is_available(self) -> bool:
        return self.initialized and self.device_count > 0

    def get_gpu_stats(self, device_index: int = 0) -> Optional[dict[str, float]]:
        if not self.is_available() or self.pynvml is None:
            return None
        if not 0 <= device_index < self.device_count:
            self.last_error = f"GPU index out of range: {device_index}"
            return None

        try:
            handle = self.pynvml.nvmlDeviceGetHandleByIndex(device_index)
            util = self.pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory = self.pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp_sensor = getattr(self.pynvml, "NVML_TEMPERATURE_GPU", 0)
            result = {
                "power_w": float(self.pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0,
                "gpu_util": float(util.gpu),
                "memory_used_mb": float(memory.used) / 1024.0 / 1024.0,
                "memory_total_mb": float(memory.total) / 1024.0 / 1024.0,
                "temperature_c": float(
                    self.pynvml.nvmlDeviceGetTemperature(handle, temp_sensor)
                ),
            }
            self.last_error = None
            return result
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def close(self) -> None:
        if self.initialized and self.pynvml is not None:
            try:
                self.pynvml.nvmlShutdown()
            except Exception:
                pass
        self.initialized = False


class PowerMonitor:
    """Unified facade for optional CPU/GPU power sources."""

    def __init__(
        self,
        *,
        hwinfo: HWiNFOReader | None = None,
        nvml: NVMLReader | None = None,
    ) -> None:
        self.hwinfo = hwinfo if hwinfo is not None else HWiNFOReader()
        self.nvml = nvml if nvml is not None else NVMLReader()

    def get_power_stats(self) -> PowerStats:
        cpu_power = self.hwinfo.get_cpu_power() if self.hwinfo.is_connected() else None
        gpu = self.nvml.get_gpu_stats() if self.nvml.is_available() else None
        return PowerStats(
            cpu_power_w=cpu_power,
            gpu_power_w=gpu.get("power_w") if gpu else None,
            gpu_util_percent=gpu.get("gpu_util") if gpu else None,
            gpu_memory_mb=gpu.get("memory_used_mb") if gpu else None,
            gpu_memory_total_mb=gpu.get("memory_total_mb") if gpu else None,
            gpu_temp_c=gpu.get("temperature_c") if gpu else None,
        )

    def close(self) -> None:
        self.hwinfo.close()
        self.nvml.close()
