"""Native Windows Service Control Manager host for the headless Agent.

The module stays import-safe off Windows and does not require pywin32. The thin
Advapi32 adapter is injectable, keeping service lifecycle semantics testable on
Linux CI while the real entry point uses documented Windows SCM APIs.
"""
from __future__ import annotations

import ctypes
import os
import threading
from typing import Any, Callable

DWORD = ctypes.c_uint32
LPVOID = ctypes.c_void_p
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_INTERROGATE = 0x00000004
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
ERROR_SUCCESS = 0
ERROR_SERVICE_SPECIFIC_ERROR = 1066

_HANDLER_EX = _WINFUNCTYPE(DWORD, DWORD, DWORD, LPVOID, LPVOID)
_SERVICE_MAIN = _WINFUNCTYPE(None, DWORD, ctypes.POINTER(ctypes.c_wchar_p))


class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", DWORD),
        ("dwCurrentState", DWORD),
        ("dwControlsAccepted", DWORD),
        ("dwWin32ExitCode", DWORD),
        ("dwServiceSpecificExitCode", DWORD),
        ("dwCheckPoint", DWORD),
        ("dwWaitHint", DWORD),
    ]


class _SERVICE_TABLE_ENTRYW(ctypes.Structure):
    # lpServiceProc is stored as void* so the NULL terminator is portable in
    # ctypes. The live callback object is retained by NativeWindowsSCM.start.
    _fields_ = [
        ("lpServiceName", ctypes.c_wchar_p),
        ("lpServiceProc", LPVOID),
    ]


def _last_error(prefix: str) -> OSError:
    getter = getattr(ctypes, "get_last_error", None)
    code = int(getter()) if callable(getter) else 0
    return OSError(code, f"{prefix} failed with Win32 error {code}")


class NativeWindowsSCM:
    """Minimal Advapi32 adapter used by :class:`WindowsServiceHost`."""

    def __init__(self, advapi32: Any | None = None) -> None:
        if advapi32 is None:
            if os.name != "nt":
                raise OSError("Windows SCM is only available on Windows")
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.dll = advapi32
        self.dll.StartServiceCtrlDispatcherW.argtypes = [
            ctypes.POINTER(_SERVICE_TABLE_ENTRYW)
        ]
        self.dll.StartServiceCtrlDispatcherW.restype = ctypes.c_int
        self.dll.RegisterServiceCtrlHandlerExW.argtypes = [
            ctypes.c_wchar_p,
            _HANDLER_EX,
            LPVOID,
        ]
        self.dll.RegisterServiceCtrlHandlerExW.restype = LPVOID
        self.dll.SetServiceStatus.argtypes = [LPVOID, ctypes.POINTER(_SERVICE_STATUS)]
        self.dll.SetServiceStatus.restype = ctypes.c_int
        self._service_main_ref = None

    def start_dispatcher(self, service_name: str, service_main) -> None:
        self._service_main_ref = service_main
        table = (_SERVICE_TABLE_ENTRYW * 2)()
        table[0].lpServiceName = service_name
        table[0].lpServiceProc = ctypes.cast(service_main, LPVOID)
        table[1].lpServiceName = None
        table[1].lpServiceProc = None
        if not self.dll.StartServiceCtrlDispatcherW(table):
            raise _last_error("StartServiceCtrlDispatcherW")

    def register_handler(self, service_name: str, handler) -> Any:
        handle = self.dll.RegisterServiceCtrlHandlerExW(service_name, handler, None)
        if not handle:
            raise _last_error("RegisterServiceCtrlHandlerExW")
        return handle

    def set_status(
        self,
        handle: Any,
        *,
        state: int,
        controls_accepted: int,
        win32_exit_code: int,
        service_exit_code: int,
        checkpoint: int,
        wait_hint_ms: int,
    ) -> None:
        status = _SERVICE_STATUS(
            SERVICE_WIN32_OWN_PROCESS,
            int(state),
            int(controls_accepted),
            int(win32_exit_code),
            int(service_exit_code),
            int(checkpoint),
            int(wait_hint_ms),
        )
        if not self.dll.SetServiceStatus(handle, ctypes.byref(status)):
            raise _last_error("SetServiceStatus")


class WindowsServiceHost:
    """SCM lifecycle state machine around a cooperative Agent runner."""

    def __init__(
        self,
        service_name: str,
        run_agent: Callable[[threading.Event], None],
        *,
        scm: Any | None = None,
        start_wait_hint_ms: int = 15_000,
        stop_wait_hint_ms: int = 15_000,
    ) -> None:
        if not service_name or "\x00" in service_name:
            raise ValueError("service_name must be non-empty and NUL-free")
        if start_wait_hint_ms <= 0 or stop_wait_hint_ms <= 0:
            raise ValueError("wait hints must be positive")
        self.service_name = service_name
        self.run_agent = run_agent
        self.scm = scm or NativeWindowsSCM()
        self.start_wait_hint_ms = int(start_wait_hint_ms)
        self.stop_wait_hint_ms = int(stop_wait_hint_ms)
        self.stop_event = threading.Event()
        self.last_error: str | None = None
        self._status_handle: Any | None = None
        self._state = SERVICE_STOPPED
        self._controls = 0
        self._checkpoint = 0
        self._lock = threading.RLock()
        # Retain callback objects for the entire dispatcher lifetime; Windows
        # holds raw function pointers and ctypes callbacks must not be GC'd.
        self._handler_callback = _HANDLER_EX(self._handle_control)
        self._service_main_callback = _SERVICE_MAIN(self._service_main)

    def _report(
        self,
        state: int,
        *,
        win32_exit_code: int = ERROR_SUCCESS,
        service_exit_code: int = 0,
        wait_hint_ms: int = 0,
    ) -> None:
        with self._lock:
            if state in {SERVICE_START_PENDING, SERVICE_STOP_PENDING}:
                self._checkpoint += 1
            else:
                self._checkpoint = 0
            controls = (
                SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
                if state == SERVICE_RUNNING
                else 0
            )
            handle = self._status_handle
            if handle is None:
                raise RuntimeError("service status handle is not registered")
            self.scm.set_status(
                handle,
                state=state,
                controls_accepted=controls,
                win32_exit_code=win32_exit_code,
                service_exit_code=service_exit_code,
                checkpoint=self._checkpoint,
                wait_hint_ms=wait_hint_ms,
            )
            self._state = state
            self._controls = controls

    def _handle_control(self, control: int, event_type: int, event_data, context) -> int:
        del event_type, event_data, context
        try:
            with self._lock:
                state = self._state
            if control in {SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN}:
                if state not in {SERVICE_STOP_PENDING, SERVICE_STOPPED}:
                    self._report(
                        SERVICE_STOP_PENDING,
                        wait_hint_ms=self.stop_wait_hint_ms,
                    )
                self.stop_event.set()
            elif control == SERVICE_CONTROL_INTERROGATE and self._status_handle is not None:
                # Interrogate does not change state. Reporting the current state
                # lets SCM refresh its view without inventing a transition.
                self.scm.set_status(
                    self._status_handle,
                    state=self._state,
                    controls_accepted=self._controls,
                    win32_exit_code=ERROR_SUCCESS,
                    service_exit_code=0,
                    checkpoint=self._checkpoint,
                    wait_hint_ms=(
                        self.stop_wait_hint_ms
                        if self._state == SERVICE_STOP_PENDING
                        else self.start_wait_hint_ms
                        if self._state == SERVICE_START_PENDING
                        else 0
                    ),
                )
        except Exception as exc:
            self.last_error = str(exc)
        return ERROR_SUCCESS

    def _service_main(self, argc: int, argv) -> None:
        del argc, argv
        exit_code = ERROR_SUCCESS
        service_exit_code = 0
        try:
            self._status_handle = self.scm.register_handler(
                self.service_name,
                self._handler_callback,
            )
            self._report(SERVICE_START_PENDING, wait_hint_ms=self.start_wait_hint_ms)
            self._report(SERVICE_RUNNING)
            self.run_agent(self.stop_event)
        except Exception as exc:
            self.last_error = str(exc)
            exit_code = ERROR_SERVICE_SPECIFIC_ERROR
            service_exit_code = 1
        finally:
            if self._status_handle is not None:
                try:
                    self._report(
                        SERVICE_STOPPED,
                        win32_exit_code=exit_code,
                        service_exit_code=service_exit_code,
                    )
                except Exception as exc:
                    self.last_error = self.last_error or str(exc)

    def run(self) -> int:
        self.stop_event.clear()
        self.last_error = None
        self._status_handle = None
        self._state = SERVICE_STOPPED
        self._controls = 0
        self._checkpoint = 0
        try:
            self.scm.start_dispatcher(self.service_name, self._service_main_callback)
        except Exception as exc:
            self.last_error = str(exc)
            return 1
        return 1 if self.last_error else 0
