"""Headless local Agent runtime independent from Qt and Docker UI lifecycle."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import threading,time
from typing import Callable
from .host_monitor import HostRuntimeWorker
from .statsd_metrics import StatsDMetricsWorker
from .storage import SQLiteTelemetryStore
from .system_metrics import SystemMetricsRuntimeWorker

@dataclass(frozen=True,slots=True)
class AgentRuntimeSnapshot:
    running: bool; ticks: int; host_samples_persisted: int; system_points_persisted: int
    host_storage_failures: int; system_storage_failures: int; retention_failures: int
    last_host_storage_error: str|None; last_system_storage_error: str|None; last_retention_error: str|None
    statsd_points_persisted: int=0; statsd_storage_failures: int=0; last_statsd_storage_error: str|None=None; statsd: object|None=None

class LocalAgentRuntime:
    def __init__(self,store:SQLiteTelemetryStore,*,host_worker:HostRuntimeWorker|None=None,system_worker:SystemMetricsRuntimeWorker|None=None,statsd_worker:StatsDMetricsWorker|None=None,drain_limit:int=1000,retention_s:float=7*24*60*60,retention_max_rows:int=250_000,retention_interval_s:float=300.0,wall_clock:Callable[[],float]=time.time,monotonic_clock:Callable[[],float]=time.monotonic)->None:
        if drain_limit<=0: raise ValueError("drain_limit must be positive")
        if retention_s<=0 or retention_interval_s<=0 or retention_max_rows<=0: raise ValueError("retention settings must be positive")
        self.store=store; self.host_worker=host_worker or HostRuntimeWorker(interval_s=1,max_queue=3600); self.system_worker=system_worker or SystemMetricsRuntimeWorker(interval_s=1,max_points=20_000,max_bytes=16*1024*1024); self.statsd_worker=statsd_worker; self.drain_limit=drain_limit; self.retention_s=retention_s; self.retention_max_rows=retention_max_rows; self.retention_interval_s=retention_interval_s; self.wall_clock=wall_clock; self.monotonic_clock=monotonic_clock
        self._running=False; self._ticks=0; self._host_persisted=self._system_persisted=self._statsd_persisted=0; self._host_storage_failures=self._system_storage_failures=self._statsd_storage_failures=self._retention_failures=0; self._last_host_storage_error=self._last_system_storage_error=self._last_statsd_storage_error=self._last_retention_error=None; self._next_retention=0.0
    def start(self):
        if self._running:return
        self.host_worker.start()
        try:
            self.system_worker.start()
            if self.statsd_worker:self.statsd_worker.start()
        except Exception:
            self.system_worker.stop(timeout_s=2); self.host_worker.stop(timeout_s=2); raise
        self._running=True; self._next_retention=self.monotonic_clock()+self.retention_interval_s
    def _drain_host(self):
        items=self.host_worker.drain(self.drain_limit)
        if not items:return
        try:self._host_persisted+=self.store.append_host_samples(items); self._last_host_storage_error=None
        except Exception as exc:self.host_worker.requeue_front(items); self._host_storage_failures+=1; self._last_host_storage_error=str(exc)
    def _drain_metrics(self,worker,system=False):
        items=worker.drain_points(self.drain_limit)
        if not items:return
        try:written=self.store.append_custom_metrics(items)
        except Exception as exc:
            worker.requeue_points(items)
            if system:self._system_storage_failures+=1; self._last_system_storage_error=str(exc)
            else:self._statsd_storage_failures+=1; self._last_statsd_storage_error=str(exc)
            return
        if system:self._system_persisted+=written; self._last_system_storage_error=None
        else:self._statsd_persisted+=written; self._last_statsd_storage_error=None
    def _retention(self):
        now=self.monotonic_clock()
        if now<self._next_retention:return
        try:self.store.prune_host_samples(before_timestamp=self.wall_clock()-self.retention_s,max_rows=self.retention_max_rows); self.store.prune_custom_metrics(before_timestamp=self.wall_clock()-self.retention_s,max_rows=self.retention_max_rows); self._last_retention_error=None
        except Exception as exc:self._retention_failures+=1; self._last_retention_error=str(exc)
        finally:self._next_retention=now+self.retention_interval_s
    def run_once(self):
        self._drain_host(); self._drain_metrics(self.system_worker,True)
        if self.statsd_worker:self._drain_metrics(self.statsd_worker)
        self._retention(); self._ticks+=1
    def run_forever(self,stop_event:threading.Event,*,poll_interval_s:float=.5):
        if poll_interval_s<=0:raise ValueError("poll_interval_s must be positive")
        self.start()
        try:
            while not stop_event.wait(poll_interval_s):self.run_once()
        finally:self.stop()
    def stop(self):
        if not self._running:return True
        a=self.host_worker.stop(timeout_s=2); b=self.system_worker.stop(timeout_s=2); c=True
        if self.statsd_worker:c=self.statsd_worker.stop(timeout_s=2)
        self.run_once(); self._running=False; return bool(a and b and c)
    def snapshot(self):
        return AgentRuntimeSnapshot(running=self._running,ticks=self._ticks,host_samples_persisted=self._host_persisted,system_points_persisted=self._system_persisted,host_storage_failures=self._host_storage_failures,system_storage_failures=self._system_storage_failures,retention_failures=self._retention_failures,last_host_storage_error=self._last_host_storage_error,last_system_storage_error=self._last_system_storage_error,last_retention_error=self._last_retention_error,statsd_points_persisted=self._statsd_persisted,statsd_storage_failures=self._statsd_storage_failures,last_statsd_storage_error=self._last_statsd_storage_error,statsd=None if self.statsd_worker is None else self.statsd_worker.snapshot())

class LocalAgentProcess:
    def __init__(self,database_path:str|Path,**runtime_kwargs):self.database_path=Path(database_path);self.runtime_kwargs=runtime_kwargs
    def run(self,stop_event:threading.Event):
        with SQLiteTelemetryStore(self.database_path) as store:LocalAgentRuntime(store,**self.runtime_kwargs).run_forever(stop_event)
