import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.workload import DockerWorkloadInspector


class FakeContainer:
    def __init__(self):
        self.top_calls = []
        self.log_calls = []
        self.raise_top = False
        self.raise_logs = False

    def top(self, **kwargs):
        self.top_calls.append(kwargs)
        if self.raise_top:
            raise RuntimeError("top unavailable")
        return {
            "Titles": ["PID", "USER", "CMD"],
            "Processes": [
                ["1", "root", "python app.py"],
                ["2", "worker"],
                ["3", "root", "sleep", "extra"],
            ],
        }

    def logs(self, **kwargs):
        self.log_calls.append(kwargs)
        if self.raise_logs:
            raise RuntimeError("logging driver unavailable")
        return (
            b"2026-09-14T12:00:00.000000000Z first\n"
            b"plain second\n"
            b"2026-09-14T12:00:02+00:00 third\n"
        )


class FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, container_id):
        if container_id != "abc":
            raise KeyError(container_id)
        return self.container


class FakeClient:
    def __init__(self, container):
        self.containers = FakeContainers(container)


class WorkloadInspectorTests(unittest.TestCase):
    def setUp(self):
        self.container = FakeContainer()
        self.inspector = DockerWorkloadInspector(FakeClient(self.container))

    def test_process_snapshot_preserves_dynamic_columns_and_bounds_rows(self):
        snapshot = self.inspector.get_processes("abc", ps_args="aux", max_rows=2)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.columns, ("PID", "USER", "CMD"))
        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(snapshot.rows[1], ("2", "worker", ""))
        self.assertEqual(self.container.top_calls, [{"ps_args": "aux"}])

    def test_process_rows_are_truncated_to_reported_columns(self):
        snapshot = self.inspector.get_processes("abc")
        self.assertEqual(snapshot.rows[2], ("3", "root", "sleep"))

    def test_process_failure_is_explicit(self):
        self.container.raise_top = True
        self.assertIsNone(self.inspector.get_processes("abc"))
        self.assertEqual(self.inspector.last_error, "top unavailable")

    def test_logs_parse_timestamps_and_preserve_unstructured_lines(self):
        rows = self.inspector.get_logs("abc", tail=50)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].timestamp, "2026-09-14T12:00:00.000000000Z")
        self.assertEqual(rows[0].message, "first")
        self.assertIsNone(rows[1].timestamp)
        self.assertEqual(rows[1].message, "plain second")
        self.assertEqual(rows[2].message, "third")
        call = self.container.log_calls[0]
        self.assertTrue(call["timestamps"])
        self.assertFalse(call["stream"])
        self.assertEqual(call["tail"], 50)

    def test_logs_apply_line_limit_after_decode(self):
        rows = self.inspector.get_logs("abc", max_lines=2)
        self.assertEqual([row.message for row in rows], ["plain second", "third"])

    def test_byte_cap_drops_partial_first_line(self):
        self.container.logs = lambda **kwargs: b"1234567890\nsecond\nthird\n"
        rows = self.inspector.get_logs("abc", max_bytes=14)
        self.assertEqual([row.message for row in rows], ["second", "third"])

    def test_invalid_utf8_is_replaced_not_fatal(self):
        self.container.logs = lambda **kwargs: b"ok\n\xffbad\n"
        rows = self.inspector.get_logs("abc")
        self.assertEqual(len(rows), 2)
        self.assertIn("bad", rows[1].message)

    def test_log_failure_distinguishes_error_from_empty_success(self):
        self.container.logs = lambda **kwargs: b""
        self.assertEqual(self.inspector.get_logs("abc"), ())
        self.assertIsNone(self.inspector.last_error)

        self.container.raise_logs = True
        self.container.logs = FakeContainer.logs.__get__(self.container, FakeContainer)
        self.assertIsNone(self.inspector.get_logs("abc"))
        self.assertEqual(self.inspector.last_error, "logging driver unavailable")

    def test_limits_reject_unbounded_requests(self):
        with self.assertRaises(ValueError):
            self.inspector.get_processes("abc", max_rows=0)
        with self.assertRaises(ValueError):
            self.inspector.get_logs("abc", tail=100001)
        with self.assertRaises(ValueError):
            self.inspector.get_logs("abc", max_bytes=0)


if __name__ == "__main__":
    unittest.main()
