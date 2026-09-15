from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_log_runtime import LogAwareAgentRuntime
from profiler.core.log_storage import ContainerLogRecord
from profiler.core.storage import SQLiteTelemetryStore


class EmptyHost:
    def start(self): pass
    def stop(self, timeout_s=2): return True
    def drain(self, limit): return ()
    def requeue_front(self, items): return 0
    def snapshot(self): return None


class EmptyMetrics:
    def start(self): pass
    def stop(self, timeout_s=2): return True
    def drain_points(self, limit): return ()
    def requeue_points(self, items): return 0
    def snapshot(self): return None


class FakeLogs:
    def __init__(self, records=()):
        self.records = list(records)
        self.started = False
        self.stopped = False

    def start(self): self.started = True
    def stop(self, timeout_s=2): self.stopped = True; return True
    def drain_records(self, limit):
        result = self.records[:limit]
        del self.records[:limit]
        return tuple(result)
    def requeue_records(self, records):
        values = list(records)
        self.records[:0] = values
        return len(values)
    def snapshot(self):
        return {"running": self.started and not self.stopped, "records_collected": 1}


class LogAwareAgentRuntimeTests(unittest.TestCase):
    def test_log_records_persist_and_snapshot_stays_status_compatible(self):
        with SQLiteTelemetryStore() as store:
            logs = FakeLogs([ContainerLogRecord(10.0, "abc", "hello", container_name="web")])
            runtime = LogAwareAgentRuntime(
                store,
                host_worker=EmptyHost(),
                system_worker=EmptyMetrics(),
                log_worker=logs,
            )
            runtime.start()
            runtime.run_once()
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot.logs_persisted, 1)
            self.assertEqual(snapshot["logs_persisted"], 1)
            self.assertEqual(snapshot.ticks, 1)
            rows = runtime.log_repository.query(container_id="abc")
            self.assertEqual(rows[0]["message"], "hello")
            self.assertTrue(runtime.stop())
            self.assertTrue(logs.stopped)

    def test_log_worker_is_optional(self):
        with SQLiteTelemetryStore() as store:
            runtime = LogAwareAgentRuntime(store, host_worker=EmptyHost(), system_worker=EmptyMetrics())
            self.assertEqual(runtime.snapshot().logs_persisted, 0)
            self.assertIsNone(runtime.snapshot().logs)


if __name__ == "__main__":
    unittest.main()
