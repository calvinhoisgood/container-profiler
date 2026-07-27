import struct
import sys
import ctypes
from ctypes import wintypes

try:
    import construct as cstruct
    from construct import Struct, Int32un, Long
except ImportError:
    print("Error: 'construct' library is not installed. Please run: pip install construct")
    sys.exit(1)

# Windows API constants and functions
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

class HWiNFOSharedMemory:
    def __init__(self):
        self.hMap = None
        self.pBuf = None
        self.size = 0
        self.connect()

    def connect(self):
        name = "Global\\HWiNFO_SENS_SM2"
        self.hMap = OpenFileMappingW(FILE_MAP_READ, False, name)
        if not self.hMap:
            raise FileNotFoundError(f"Could not open HWiNFO shared memory. Error code: {ctypes.get_last_error()}")

        # Map the whole file (0 means whole file)
        self.pBuf = MapViewOfFile(self.hMap, FILE_MAP_READ, 0, 0, 0)
        if not self.pBuf:
            CloseHandle(self.hMap)
            self.hMap = None
            raise OSError(f"Could not map view of file. Error code: {ctypes.get_last_error()}")

    def read_buffer(self, offset, size):
        if not self.pBuf:
            raise OSError("Shared memory not connected")
        
        # Create a buffer from the memory address
        # We use ctypes to access the memory at pBuf + offset
        address = self.pBuf + offset
        return (ctypes.c_byte * size).from_address(address)

    def close(self):
        if self.pBuf:
            UnmapViewOfFile(self.pBuf)
            self.pBuf = None
        if self.hMap:
            CloseHandle(self.hMap)
            self.hMap = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def main():
    # Set stdout encoding to utf-8 to avoid printing errors
    sys.stdout.reconfigure(encoding='utf-8')
    
    try:
        with HWiNFOSharedMemory() as memory:
            # size = 44
            sensor_element_struct = Struct(
                'dwSignature' / Int32un,
                'dwVersion' / Int32un,
                'dwRevision' / Int32un,
                'poll_time' / Long,
                'dwOffsetOfSensorSection' / Int32un,
                'dwSizeOfSensorElement' / Int32un,
                'dwNumSensorElements' / Int32un,
                'dwOffsetOfReadingSection' / Int32un,
                'dwSizeOfReadingElement' / Int32un,
                'dwNumReadingElements' / Int32un,
            )

            # Read header
            header_size = sensor_element_struct.sizeof()
            header_buf = memory.read_buffer(0, header_size)
            sensor_element = sensor_element_struct.parse(header_buf)
            
            # print(f"HWiNFO Signature: {hex(sensor_element.dwSignature)} (Should be 0x48576953 'HWiS')")
            # print(f"Sensors: {sensor_element.dwNumSensorElements}, Readings: {sensor_element.dwNumReadingElements}")

            fmt = '=III128s128s16sdddd'
            reading_element_struct = struct.Struct(fmt)
            struct_size = reading_element_struct.size
            
            offset = sensor_element.dwOffsetOfReadingSection
            length = sensor_element.dwSizeOfReadingElement
            num_readings = sensor_element.dwNumReadingElements
            
            # print(f"Element Size from HWiNFO: {length} bytes")
            # print(f"Expected Struct Size: {struct_size} bytes")

            # print(f"\nReading {num_readings} elements...")

            found = False
            for index in range(num_readings):
                start = offset + index * length
                
                read_size = min(length, struct_size)
                reading_buf = memory.read_buffer(start, read_size)
                
                try:
                    reading = reading_element_struct.unpack(reading_buf)
                    
                    # reading structure based on fmt:
                    # 0: tReading (I)
                    # 1: dwSensorIndex (I)
                    # 2: dwReadingID (I)
                    # 3: szLabelOrig (128s)
                    # 4: szLabelUser (128s)
                    # 5: szUnit (16s)
                    # 6: Value (d)
                    
                    label_orig = reading[3].replace(b'\x00', b'').decode('utf-8', errors='ignore')
                    label_user = reading[4].replace(b'\x00', b'').decode('utf-8', errors='ignore')
                    unit = reading[5].replace(b'\x00', b'').decode('mbcs', errors='ignore')
                    
                    # =========================================================================
                    # [過濾條件區域] FILTERING CONDITION AREA
                    # =========================================================================
                    # 這裏控制要顯示哪些傳感器數據。
                    # 您可以修改字符串 "CPU Package Power" 為其他內容，例如 "GPU Temperature" 或 "Total Activity"。
                    # label_orig: 原始英文標籤 (例如 "CPU Package Power")
                    # label_user: 用戶自定義標籤 (如果有的話，否則同上)
                    
                    if "CPU Package Power" in label_orig or "CPU Package Power" in label_user:
                        print(f"ID: {reading[2]}, Label: {label_user} ({label_orig}), Value: {reading[6]} {unit}")
                        found = True
                    
                    # 如果想顯示所有數據，請注釋掉上面的 if 塊，並取消下面這行的注釋：
                    # print(f"ID: {reading[2]}, Label: {label_user} ({label_orig}), Value: {reading[6]} {unit}")
                    
                    # =========================================================================
                        
                except struct.error as e:
                     # print(f"Error unpacking index {index}: {e}")
                     break
            
            if not found:
                 print("Warning: 'CPU Package Power' not found. Please check HWiNFO sensors.")

    except FileNotFoundError:
        print("Error: Could not find HWiNFO shared memory.")
        print("Please ensure:")
        print("1. HWiNFO is running.")
        print("2. 'Shared Memory Support' is enabled in HWiNFO settings.")
    except Exception as e:
        print(f"An error occurred: {e}")
        # import traceback
        # traceback.print_exc()

if __name__ == "__main__":
    main()
