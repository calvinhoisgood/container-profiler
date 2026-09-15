import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import agent_service
from profiler.core.agent_forwarding import AgentForwardingRuntime
from profiler.core.agent_openmetrics import AgentOpenMetricsWorker
from profiler.core.container_logs import ContainerLogWorker
from profiler.core.container_metrics import ContainerMetricsWorker
from profiler.core.statsd_metrics import StatsDMetricsWorker


class AgentServiceCLITests(unittest.TestCase):
    def test_binary_command_is_windows_quoted_and_contains_service_run_mode(self):
        database = Path(r"C:\Program Data\Container Profiler\telemetry.sqlite3")
        with mock.patch.object(agent_service.sys, "frozen", False, create=True):
            command = agent_service.build_service_binary_command(database)
        expected = subprocess.list2cmdline([
            sys.executable,
            str(Path(agent_service.__file__).resolve()),
            "run",
            "--database",
            str(database),
        ])
        self.assertEqual(command, expected)
        self.assertIn('"C:\\Program Data\\Container Profiler\\telemetry.sqlite3"', command)

    def test_container_logs_require_explicit_persistent_service_opt_in(self):
        database = Path(r"C:\Program Data\Container Profiler\telemetry.sqlite3")
        command = agent_service.build_service_binary_command(database, container_logs=True)
        self.assertIn("--container-logs", command)
        default_kwargs = agent_service._service_runtime_kwargs()
        opted_in = agent_service._service_runtime_kwargs(container_logs=True)
        self.assertNotIn("log_worker", default_kwargs)
        self.assertIsInstance(opted_in["log_worker"], ContainerLogWorker)
        self.assertIsNone(opted_in["log_worker"].monitor)

    def test_service_composes_full_headless_agent_without_starting_hardware(self):
        kwargs = agent_service._service_runtime_kwargs()
        self.assertIsInstance(kwargs["container_worker"], ContainerMetricsWorker)
        self.assertIsInstance(kwargs["statsd_worker"], StatsDMetricsWorker)
        self.assertIsInstance(kwargs["openmetrics_worker"], AgentOpenMetricsWorker)
        self.assertIsInstance(kwargs["forwarding_worker"], AgentForwardingRuntime)
        self.assertIsNone(kwargs["container_worker"].monitor)
        self.assertIsNone(kwargs["openmetrics_worker"].monitor)
        self.assertFalse(kwargs["statsd_worker"].server.is_running)
        self.assertFalse(kwargs["forwarding_worker"].snapshot().running)

    def test_run_service_passes_full_runtime_to_process(self):
        process = mock.Mock()
        host = mock.Mock()
        host.run.return_value = 0
        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "LogAwareAgentProcess", return_value=process
        ) as local_process, mock.patch.object(
            agent_service, "WindowsServiceHost", return_value=host
        ):
            result = agent_service._run_service(Path("telemetry.sqlite3"))
        self.assertEqual(result, 0)
        kwargs = local_process.call_args.kwargs
        self.assertIn("container_worker", kwargs)
        self.assertIn("statsd_worker", kwargs)
        self.assertIn("openmetrics_worker", kwargs)
        self.assertIn("forwarding_worker", kwargs)
        self.assertNotIn("log_worker", kwargs)

    def test_run_service_adds_logs_only_after_opt_in(self):
        process = mock.Mock()
        host = mock.Mock()
        host.run.return_value = 0
        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "LogAwareAgentProcess", return_value=process
        ) as local_process, mock.patch.object(
            agent_service, "WindowsServiceHost", return_value=host
        ):
            result = agent_service._run_service(Path("telemetry.sqlite3"), container_logs=True)
        self.assertEqual(result, 0)
        self.assertIsInstance(local_process.call_args.kwargs["log_worker"], ContainerLogWorker)

    def test_install_refuses_machine_change_without_explicit_confirmation(self):
        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "_run_sc"
        ) as run_sc:
            result = agent_service._install(Path("telemetry.sqlite3"), confirmed=False)
        self.assertEqual(result, 2)
        run_sc.assert_not_called()

    def test_install_uses_builtin_sc_and_auto_start_after_confirmation(self):
        calls = []

        def fake_sc(*args):
            calls.append(args)
            return 0

        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "_run_sc", side_effect=fake_sc
        ):
            result = agent_service._install(Path("telemetry.sqlite3"), confirmed=True)
        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0:2], ("create", agent_service.SERVICE_NAME))
        self.assertIn("binPath=", calls[0])
        self.assertIn("start=", calls[0])
        self.assertIn("auto", calls[0])
        self.assertEqual(calls[1][0:2], ("description", agent_service.SERVICE_NAME))

    def test_install_persists_log_opt_in_in_scm_command(self):
        calls = []
        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "_run_sc", side_effect=lambda *args: calls.append(args) or 0
        ):
            result = agent_service._install(
                Path("telemetry.sqlite3"), confirmed=True, container_logs=True
            )
        self.assertEqual(result, 0)
        self.assertIn("--container-logs", " ".join(calls[0]))

    def test_uninstall_also_requires_explicit_confirmation(self):
        with mock.patch.object(agent_service, "_windows_required", return_value=True), mock.patch.object(
            agent_service, "_run_sc"
        ) as run_sc:
            self.assertEqual(agent_service._uninstall(confirmed=False), 2)
            run_sc.assert_not_called()
            run_sc.return_value = 0
            self.assertEqual(agent_service._uninstall(confirmed=True), 0)
            run_sc.assert_called_once_with("delete", agent_service.SERVICE_NAME)

    def test_print_install_command_is_safe_on_non_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "telemetry.sqlite3"
            with mock.patch("builtins.print") as printer:
                result = agent_service.main([
                    "print-install-command",
                    "--database",
                    str(database),
                ])
        self.assertEqual(result, 0)
        printer.assert_called_once()
        self.assertIn("run", printer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
