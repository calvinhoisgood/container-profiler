import os
import sys
import unittest

from profiler.core.host_process import LinuxProcProcessBackend, WindowsToolhelpProcessBackend


class NativeProcessSmokeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux procfs only")
    def test_linux_procfs_native_snapshot(self):
        stats = LinuxProcProcessBackend().read()
        self.assertGreater(stats.processes, 0)
        self.assertGreaterEqual(stats.threads, stats.processes)
        self.assertGreaterEqual(stats.skipped_processes, 0)
        state_total = sum(
            value
            for value in (
                stats.running,
                stats.sleeping,
                stats.blocked,
                stats.stopped,
                stats.zombies,
                stats.unknown,
            )
            if value is not None
        )
        self.assertEqual(state_total, stats.processes)

    @unittest.skipUnless(os.name == "nt", "Windows Tool Help only")
    def test_windows_toolhelp_native_snapshot(self):
        stats = WindowsToolhelpProcessBackend().read()
        self.assertGreater(stats.processes, 0)
        self.assertGreaterEqual(stats.threads, stats.processes)
        self.assertIsNone(stats.running)
        self.assertEqual(stats.skipped_processes, 0)


if __name__ == "__main__":
    unittest.main()
