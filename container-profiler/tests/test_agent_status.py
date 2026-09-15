import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.agent_runtime import AgentRuntimeSnapshot
from profiler.core.agent_status import read_agent_status, write_agent_status


def snapshot(**overrides):
    values = {
        "running": True,
        "ticks": 12,
        "host_samples_persisted": 10,
        "system_points_persisted": 20,
        "host_storage_failures": 0,
        "system_storage_failures": 0,
        "retention_failures": 0,
        "last_host_storage_error": None,
        "last_system_storage_error": None,
        "last_retention_error": None,
    }
    values.update(overrides)
    return AgentRuntimeSnapshot(**values)


class AgentStatusTests(unittest.TestCase):
    def test_atomic_round_trip_and_fresh_running_health(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-status.json"
            write_agent_status(
                path,
                state="running",
                pid=123,
                started_at=90.0,
                updated_at=100.0,
                runtime_snapshot=snapshot(),
                database_path=Path(directory) / "telemetry.sqlite3",
            )
            view = read_agent_status(path, max_age_s=15.0, now=110.0)
            self.assertEqual(view.state, "running")
            self.assertEqual(view.pid, 123)
            self.assertEqual(view.age_s, 10.0)
            self.assertFalse(view.stale)
            self.assertEqual(view.runtime["ticks"], 12)
            leftovers = [item for item in Path(directory).iterdir() if item.suffix == ".tmp"]
            self.assertEqual(leftovers, [])

    def test_old_running_heartbeat_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_agent_status(
                path,
                state="running",
                pid=1,
                started_at=1.0,
                updated_at=10.0,
                runtime_snapshot=snapshot(),
                database_path="db.sqlite3",
            )
            view = read_agent_status(path, max_age_s=5.0, now=20.0)
            self.assertTrue(view.stale)
            self.assertEqual(view.age_s, 10.0)

    def test_stopped_status_is_stale_even_when_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_agent_status(
                path,
                state="stopped",
                pid=1,
                started_at=1.0,
                updated_at=10.0,
                runtime_snapshot=snapshot(running=False),
                database_path="db.sqlite3",
            )
            view = read_agent_status(path, max_age_s=30.0, now=11.0)
            self.assertEqual(view.state, "stopped")
            self.assertTrue(view.stale)

    def test_mapping_snapshot_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_agent_status(
                path,
                state="running",
                pid=5,
                started_at=1.0,
                updated_at=2.0,
                runtime_snapshot={"ticks": 7, "last_error": "degraded"},
                database_path="db.sqlite3",
            )
            view = read_agent_status(path, max_age_s=5.0, now=3.0)
            self.assertEqual(view.runtime["ticks"], 7)
            self.assertEqual(view.runtime["last_error"], "degraded")

    def test_malformed_and_future_schema_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                read_agent_status(path)
            path.write_text(
                json.dumps({"schema_version": 99, "state": "running", "runtime": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                read_agent_status(path)

    def test_invalid_writer_inputs_fail_before_replacing_existing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text("sentinel", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_agent_status(
                    path,
                    state="unknown",
                    pid=1,
                    started_at=1.0,
                    runtime_snapshot={},
                    database_path="db.sqlite3",
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
