import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.windows_service import (
    ERROR_SERVICE_SPECIFIC_ERROR,
    SERVICE_ACCEPT_SHUTDOWN,
    SERVICE_ACCEPT_STOP,
    SERVICE_CONTROL_INTERROGATE,
    SERVICE_CONTROL_STOP,
    SERVICE_RUNNING,
    SERVICE_START_PENDING,
    SERVICE_STOPPED,
    SERVICE_STOP_PENDING,
    WindowsServiceHost,
)


class FakeSCM:
    def __init__(self):
        self.handler = None
        self.statuses = []
        self.dispatch_name = None
        self.dispatch_fail = None

    def start_dispatcher(self, service_name, service_main):
        if self.dispatch_fail:
            raise OSError(self.dispatch_fail)
        self.dispatch_name = service_name
        service_main(0, None)

    def register_handler(self, service_name, handler):
        self.handler = handler
        return 123

    def set_status(self, handle, **status):
        self.statuses.append({"handle": handle, **status})


class WindowsServiceHostTests(unittest.TestCase):
    def test_service_reports_start_running_stop_pending_stopped(self):
        scm = FakeSCM()

        def run_agent(stop_event):
            self.assertFalse(stop_event.is_set())
            scm.handler(SERVICE_CONTROL_STOP, 0, None, None)
            self.assertTrue(stop_event.is_set())

        host = WindowsServiceHost("ContainerProfilerAgent", run_agent, scm=scm)
        self.assertEqual(host.run(), 0)
        self.assertEqual(scm.dispatch_name, "ContainerProfilerAgent")
        states = [item["state"] for item in scm.statuses]
        self.assertEqual(
            states,
            [SERVICE_START_PENDING, SERVICE_RUNNING, SERVICE_STOP_PENDING, SERVICE_STOPPED],
        )
        running = scm.statuses[1]
        self.assertEqual(
            running["controls_accepted"],
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN,
        )
        self.assertGreater(scm.statuses[0]["checkpoint"], 0)
        self.assertGreater(scm.statuses[2]["checkpoint"], 0)
        self.assertEqual(scm.statuses[-1]["checkpoint"], 0)

    def test_interrogate_republishes_state_without_transition(self):
        scm = FakeSCM()

        def run_agent(stop_event):
            before = len(scm.statuses)
            scm.handler(SERVICE_CONTROL_INTERROGATE, 0, None, None)
            self.assertEqual(len(scm.statuses), before + 1)
            self.assertEqual(scm.statuses[-1]["state"], SERVICE_RUNNING)
            stop_event.set()

        host = WindowsServiceHost("ContainerProfilerAgent", run_agent, scm=scm)
        self.assertEqual(host.run(), 0)
        states = [item["state"] for item in scm.statuses]
        self.assertEqual(states[:3], [SERVICE_START_PENDING, SERVICE_RUNNING, SERVICE_RUNNING])
        self.assertEqual(states[-1], SERVICE_STOPPED)

    def test_agent_failure_reports_service_specific_error(self):
        scm = FakeSCM()

        def run_agent(_stop_event):
            raise RuntimeError("collector bootstrap failed")

        host = WindowsServiceHost("ContainerProfilerAgent", run_agent, scm=scm)
        self.assertEqual(host.run(), 1)
        self.assertIn("collector bootstrap failed", host.last_error)
        stopped = scm.statuses[-1]
        self.assertEqual(stopped["state"], SERVICE_STOPPED)
        self.assertEqual(stopped["win32_exit_code"], ERROR_SERVICE_SPECIFIC_ERROR)
        self.assertEqual(stopped["service_exit_code"], 1)

    def test_dispatcher_failure_is_returned_without_faking_service_status(self):
        scm = FakeSCM()
        scm.dispatch_fail = "not connected to service controller"
        host = WindowsServiceHost("ContainerProfilerAgent", lambda event: None, scm=scm)
        self.assertEqual(host.run(), 1)
        self.assertEqual(scm.statuses, [])
        self.assertIn("not connected", host.last_error)

    def test_validation_rejects_invalid_name_and_wait_hints(self):
        scm = FakeSCM()
        with self.assertRaises(ValueError):
            WindowsServiceHost("", lambda event: None, scm=scm)
        with self.assertRaises(ValueError):
            WindowsServiceHost("bad\x00name", lambda event: None, scm=scm)
        with self.assertRaises(ValueError):
            WindowsServiceHost(
                "ContainerProfilerAgent",
                lambda event: None,
                scm=scm,
                start_wait_hint_ms=0,
            )


if __name__ == "__main__":
    unittest.main()
