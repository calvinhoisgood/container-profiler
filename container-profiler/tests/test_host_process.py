import unittest

from profiler.core.host_process import (
    LinuxProcProcessBackend,
    NativeProcessMonitor,
    ProcessSummary,
    WindowsToolhelpProcessBackend,
)


def stat_line(pid, comm, state, threads):
    # fields 4..19 can be arbitrary integers for this parser; field 20 is threads.
    return f"{pid} ({comm}) {state} " + " ".join(["1"] * 16 + [str(threads)])


class LinuxProcessBackendTests(unittest.TestCase):
    def test_parse_stat_handles_spaces_and_closing_parenthesis_in_comm(self):
        state, threads = LinuxProcProcessBackend._parse_stat(
            stat_line(123, "worker ) pool", "R", 7)
        )
        self.assertEqual(state, "R")
        self.assertEqual(threads, 7)

    def test_read_summarizes_states_and_skips_process_races(self):
        payloads = {
            "1": stat_line(1, "run", "R", 2),
            "2": stat_line(2, "sleep", "S", 3),
            "3": stat_line(3, "blocked", "D", 1),
            "4": stat_line(4, "stop", "T", 4),
            "5": stat_line(5, "zombie", "Z", 1),
            "6": stat_line(6, "idle", "I", 2),
            "7": stat_line(7, "odd", "X", 5),
        }

        def reader(path):
            pid = str(path).split("/")[-2]
            if pid == "8":
                raise FileNotFoundError(pid)
            return payloads[pid]

        backend = LinuxProcProcessBackend(
            proc_root="/fake-proc",
            wall_clock=lambda: 123.5,
            listdir=lambda root: [*payloads, "8", "self", "thread-self"],
            reader=reader,
        )
        stats = backend.read()
        self.assertEqual(stats.timestamp, 123.5)
        self.assertEqual(stats.processes, 7)
        self.assertEqual(stats.threads, 18)
        self.assertEqual(stats.running, 1)
        self.assertEqual(stats.sleeping, 2)
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.stopped, 1)
        self.assertEqual(stats.zombies, 1)
        self.assertEqual(stats.unknown, 1)
        self.assertEqual(stats.skipped_processes, 1)

    def test_bad_stat_is_bounded_as_skip_not_total_failure(self):
        backend = LinuxProcProcessBackend(
            proc_root="/fake",
            listdir=lambda root: ["1", "2"],
            reader=lambda path: (
                "bad" if str(path).endswith("1/stat") else stat_line(2, "ok", "S", 2)
            ),
        )
        stats = backend.read()
        self.assertEqual(stats.processes, 1)
        self.assertEqual(stats.threads, 2)
        self.assertEqual(stats.skipped_processes, 1)


class WindowsProcessBackendTests(unittest.TestCase):
    def test_entry_summary_counts_processes_and_threads_without_per_pid_series(self):
        stats = WindowsToolhelpProcessBackend._summarize_entries(
            [(4, 10), (10, 2), (20, 5)], timestamp=7.0
        )
        self.assertEqual(stats.processes, 3)
        self.assertEqual(stats.threads, 17)
        self.assertIsNone(stats.running)
        self.assertEqual(stats.timestamp, 7.0)


class NativeProcessMonitorTests(unittest.TestCase):
    def test_backend_failure_is_isolated(self):
        class Failing:
            def read(self):
                raise OSError("process snapshot denied")

        monitor = NativeProcessMonitor(Failing())
        self.assertIsNone(monitor.get_stats())
        self.assertEqual(monitor.last_error, "process snapshot denied")

    def test_success_clears_previous_error(self):
        class Sequence:
            def __init__(self):
                self.calls = 0

            def read(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("temporary")
                return ProcessSummary(1.0, 2, 3)

        monitor = NativeProcessMonitor(Sequence())
        self.assertIsNone(monitor.get_stats())
        self.assertIsNotNone(monitor.last_error)
        self.assertEqual(monitor.get_stats(), ProcessSummary(1.0, 2, 3))
        self.assertIsNone(monitor.last_error)


if __name__ == "__main__":
    unittest.main()
