"""
功耗監控模組 - HWiNFO + NVIDIA NVML
"""
import ctypes
from ctypes import wintypes
import struct
from dataclasses import dataclass
from typing import Optional

@dataclass
class PowerStats:
    """功耗統計"""
    cpu_power_w: Optional[float] = None  # CPU 封裝功耗
    gpu_power_w: Optional[float] = None  # GPU 功耗
    gpu_util_percent: Optional[float] = None  # GPU 利用率
    gpu_memory_mb: Optional[float] = None  # GPU 顯存使用
    gpu_temp_c: Optional[float] = None  # GPU 溫度

# Windows API Constants & Types
FILE_MAP_READ = 0x0004
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

OpenFileMappingW = kernel32.OpenFileMappingW
OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
OpenFileMappingW.restype = wintypes.HANDLE

MapViewOfFile = kernel32.MapViewOfFile
MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
MapViewOfFile.restype = wintypes.LPVOID

UnmapViewOfFile = kernel32.UnmapViewOfFile
UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
UnmapViewOfFile.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

class HWiNFOReader:
    """HWiNFO 共享內存讀取器"""
    
    HWINFO_SENSORS_SM_NAME = "Global\HWiNFO_SENS_SM2"
    
    def __init__(self):
        self.hMap = None
        self.pBuf = None
        self._connect()
    
    def _connect(self):
        """連接 HWiNFO 共享內存"""
        try:
            self.hMap = OpenFileMappingW(FILE_MAP_READ, False, self.HWINFO_SENSORS_SM_NAME)
            if not self.hMap:
                print(f"HWiNFO 連接失敗: 無法打開文件映射 ({ctypes.get_last_error()})")
                return

            self.pBuf = MapViewOfFile(self.hMap, FILE_MAP_READ, 0, 0, 0)
            if not self.pBuf:
                print(f"HWiNFO 連接失敗: 無法映射視圖 ({ctypes.get_last_error()})")
                CloseHandle(self.hMap)
                self.hMap = None
                return
                
            print("HWiNFO 連接成功")
            
        except Exception as e:
            print(f"HWiNFO 初始化異常: {e}")
            self.pBuf = None
    
    def is_connected(self) -> bool:
        return self.pBuf is not None
    
    def _read_buffer(self, offset, size):
        if not self.pBuf:
            return None
        address = self.pBuf + offset
        return (ctypes.c_byte * size).from_address(address)

    def get_cpu_power(self) -> Optional[float]:
        """讀取 CPU Package Power"""
        if not self.pBuf:
            return None
            
        try:
            # Header Format: '=IIIQIIIIII' (44 bytes, standard alignment)
            # Signature, Version, Revision, PollTime, OffsetSensor, SizeSensor, NumSensor, OffsetReading, SizeReading, NumReading
            header_size = 44
            header_buf = self._read_buffer(0, header_size)
            if not header_buf:
                return None
                
            header_data = struct.unpack('=IIIQIIIIII', header_buf)
            
            offset_reading_section = header_data[7]
            size_reading_element = header_data[8]
            num_reading_elements = header_data[9]
            
            # Reading Element Format: '=III128s128s16sdddd'
            # 0: tReading (I)
            # 1: dwSensorIndex (I)
            # 2: dwReadingID (I)
            # 3: szLabelOrig (128s)
            # 4: szLabelUser (128s)
            # 5: szUnit (16s)
            # 6: Value (d)
            # 7: ValueMin (d)
            # 8: ValueMax (d)
            # 9: ValueAvg (d)
            reading_fmt = '=III128s128s16sdddd'
            reading_struct = struct.Struct(reading_fmt)
            struct_size = reading_struct.size
            
            # 遍歷所有 Reading Elements
            for i in range(num_reading_elements):
                start = offset_reading_section + i * size_reading_element
                read_size = min(size_reading_element, struct_size)
                
                reading_buf = self._read_buffer(start, read_size)
                if not reading_buf:
                    continue
                    
                reading = reading_struct.unpack(reading_buf)
                
                label_orig = reading[3].replace(b'\x00', b'').decode('utf-8', errors='ignore')
                label_user = reading[4].replace(b'\x00', b'').decode('utf-8', errors='ignore')
                
                # 模糊匹配 CPU Package Power
                target = "CPU Package Power"
                if target in label_orig or target in label_user:
                    # print(f"Found: {label_orig} = {reading[6]}")
                    return reading[6] # Value
                    
            return None
            
        except Exception as e:
            # print(f"HWiNFO 讀取異常: {e}")
            return None
    
    def close(self):
        if self.pBuf:
            UnmapViewOfFile(self.pBuf)
            self.pBuf = None
        if self.hMap:
            CloseHandle(self.hMap)
            self.hMap = None

class NVMLReader:
    """NVIDIA GPU 監控器"""
    
    def __init__(self):
        self.initialized = False
        self._init()
    
    def _init(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            self.pynvml = pynvml
            self.device_count = pynvml.nvmlDeviceGetCount()
            self.initialized = True
        except Exception:
            self.initialized = False
    
    def is_available(self) -> bool:
        return self.initialized and self.device_count > 0
    
    def get_gpu_stats(self, device_index: int = 0) -> Optional[dict]:
        """獲取 GPU 統計"""
        if not self.initialized:
            return None
        
        try:
            handle = self.pynvml.nvmlDeviceGetHandleByIndex(device_index)
            
            power = self.pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW -> W
            util = self.pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory = self.pynvml.nvmlDeviceGetMemoryInfo(handle)
            temp = self.pynvml.nvmlDeviceGetTemperature(handle, 0)
            
            return {
                "power_w": round(power, 2),
                "gpu_util": util.gpu,
                "memory_used_mb": round(memory.used / 1024 / 1024, 2),
                "memory_total_mb": round(memory.total / 1024 / 1024, 2),
                "temperature_c": temp
            }
        except Exception as e:
            print(f"讀取 GPU 統計失敗: {e}")
            return None
    
    def close(self):
        if self.initialized:
            try:
                self.pynvml.nvmlShutdown()
            except:
                pass

class PowerMonitor:
    """統一功耗監控接口"""
    
    def __init__(self):
        self.hwinfo: Optional[HWiNFOReader] = HWiNFOReader()
        self.nvml: Optional[NVMLReader] = NVMLReader()
    
    def get_power_stats(self) -> PowerStats:
        """獲取功耗統計"""
        stats = PowerStats()
        
        # CPU 功耗
        if self.hwinfo and self.hwinfo.is_connected():
            stats.cpu_power_w = self.hwinfo.get_cpu_power()
        
        # GPU 統計
        if self.nvml and self.nvml.is_available():
            gpu_stats = self.nvml.get_gpu_stats()
            if gpu_stats:
                stats.gpu_power_w = gpu_stats["power_w"]
                stats.gpu_util_percent = gpu_stats["gpu_util"]
                stats.gpu_memory_mb = gpu_stats["memory_used_mb"]
                stats.gpu_temp_c = gpu_stats["temperature_c"]
        
        return stats
    
    def close(self):
        if self.hwinfo:
            self.hwinfo.close()
        if self.nvml:
            self.nvml.close()