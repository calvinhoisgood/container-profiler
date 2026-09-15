import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.power_monitor import DisabledCPUPowerReader, PowerMonitor


class NoGPU:
    def is_available(self):
        return False

    def get_gpu_stats(self):
        raise AssertionError("unavailable GPU backend should not be queried")

    def close(self):
        pass


class PowerProviderPolicyTests(unittest.TestCase):
    def test_hwinfo_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            monitor = PowerMonitor(nvml=NoGPU())
        self.assertIsInstance(monitor.hwinfo, DisabledCPUPowerReader)
        stats = monitor.get_power_stats()
        self.assertIsNone(stats.cpu_power_w)
        monitor.close()

    def test_false_environment_value_does_not_enable_hwinfo(self):
        with patch.dict(os.environ, {"CONTAINER_PROFILER_ENABLE_HWINFO": "false"}, clear=True):
            monitor = PowerMonitor(nvml=NoGPU())
        self.assertIsInstance(monitor.hwinfo, DisabledCPUPowerReader)
        monitor.close()


if __name__ == "__main__":
    unittest.main()
