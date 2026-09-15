import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.host_filesystem import (
    LinuxFilesystemBackend,
    NativeFilesystemMonitor,
    WindowsFilesystemBackend,
    _decode_mount_field,
)


class LinuxFilesystemTests(unittest.TestCase):
    def test_mount_parsing_capacity_and_virtual_fs_filtering(self):
        mounts = (
            "/dev/sda1 / ext4 rw 0 0\n"
            "tmpfs /run tmpfs rw 0 0\n"
            "/dev/sdb1 /data\\040space xfs rw 0 0\n"
        )
        values = {
            "/": SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=100, f_bavail=25),
            "/data space": SimpleNamespace(f_frsize=1024, f_bsize=1024, f_blocks=200, f_bavail=50),
        }
        backend = LinuxFilesystemBackend(
            mounts_reader=lambda: mounts,
            statvfs=lambda path: values[path],
            wall_clock=lambda: 10.0,
        )
        stats = backend.read()
        self.assertEqual([item.mountpoint for item in stats], ["/", "/data space"])
        self.assertEqual(stats[0].total_bytes, 409600)
        self.assertEqual(stats[0].free_bytes, 102400)
        self.assertEqual(stats[0].used_percent, 75.0)
        self.assertEqual(stats[1].filesystem, "xfs")

    def test_inaccessible_mount_is_isolated_and_result_is_bounded(self):
        mounts = "/dev/a /a ext4 rw 0 0\n/dev/b /b ext4 rw 0 0\n/dev/c /c ext4 rw 0 0\n"
        def statvfs(path):
            if path == "/a":
                raise OSError("gone")
            return SimpleNamespace(f_frsize=1, f_bsize=1, f_blocks=10, f_bavail=5)
        stats = LinuxFilesystemBackend(
            mounts_reader=lambda: mounts, statvfs=statvfs, max_filesystems=1
        ).read()
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0].mountpoint, "/b")

    def test_proc_escape_decoder(self):
        self.assertEqual(_decode_mount_field("/a\\040b\\134c"), "/a b\\c")


class FakeKernel32:
    def GetLogicalDrives(self):
        return (1 << 2) | (1 << 3)  # C and D

    def GetDiskFreeSpaceExW(self, root, available, total, free_total):
        if root == "D:\\":
            return 0
        available._obj.value = 25
        total._obj.value = 100
        free_total._obj.value = 30
        return 1


class WindowsFilesystemTests(unittest.TestCase):
    def test_kernel32_capacity_and_offline_drive_isolation(self):
        stats = WindowsFilesystemBackend(
            kernel32=FakeKernel32(), wall_clock=lambda: 20.0
        ).read()
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0].mountpoint, "C:\\")
        self.assertEqual(stats[0].total_bytes, 100)
        self.assertEqual(stats[0].free_bytes, 25)
        self.assertEqual(stats[0].used_percent, 75.0)


class BrokenBackend:
    def read(self):
        raise RuntimeError("filesystem API failed")


class MonitorTests(unittest.TestCase):
    def test_failure_degrades_without_raising(self):
        monitor = NativeFilesystemMonitor(BrokenBackend())
        self.assertEqual(monitor.get_stats(), ())
        self.assertEqual(monitor.last_error, "filesystem API failed")


if __name__ == "__main__":
    unittest.main()
