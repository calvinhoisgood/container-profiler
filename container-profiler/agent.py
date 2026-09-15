"""Headless Container Profiler Agent entry point; imports no Qt modules."""
from __future__ import annotations
import argparse, logging, os, signal, sys, threading, time
from pathlib import Path
if not getattr(sys,"frozen",False): sys.path.insert(0,str(Path(__file__).resolve().parent/"src"))
from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.agent_status import read_agent_status, write_agent_status
from profiler.core.statsd_metrics import StatsDMetricsWorker
from profiler.core.storage import SQLiteTelemetryStore
from profiler.utils.paths import get_agent_status_path,get_telemetry_db_path

def build_parser():
    p=argparse.ArgumentParser(description="Container Profiler headless agent")
    p.add_argument("--database",type=Path,default=get_telemetry_db_path())
    p.add_argument("--poll-interval",type=float,default=0.5)
    p.add_argument("--status-file",type=Path,default=get_agent_status_path())
    p.add_argument("--heartbeat-interval",type=float,default=5.0)
    p.add_argument("--status",action="store_true")
    p.add_argument("--status-max-age",type=float,default=15.0)
    p.add_argument("--dogstatsd-host",default="127.0.0.1",help="DogStatsD bind host; loopback by default")
    p.add_argument("--dogstatsd-port",type=int,default=8125)
    p.add_argument("--dogstatsd-flush-interval",type=float,default=10.0)
    p.add_argument("--no-dogstatsd",action="store_true",help="Disable local DogStatsD UDP ingestion")
    p.add_argument("--log-level",choices=("DEBUG","INFO","WARNING","ERROR"),default="INFO")
    return p

def _install_signal_handlers(stop_event):
    def request_stop(signum,frame):
        del frame; logging.getLogger("profiler.agent").info("shutdown requested by signal %s",signum); stop_event.set()
    for name in ("SIGINT","SIGTERM","SIGBREAK"):
        sig=getattr(signal,name,None)
        if sig is not None:
            try: signal.signal(sig,request_stop)
            except (OSError,ValueError): pass

def _print_status(path,max_age_s):
    try: view=read_agent_status(path,max_age_s=max_age_s)
    except FileNotFoundError: print(f"agent status unavailable: {path} does not exist",file=sys.stderr); return 3
    except Exception as exc: print(f"agent status invalid: {exc}",file=sys.stderr); return 2
    r=view.runtime; age="-" if view.age_s is None else f"{view.age_s:.1f}s"; health="healthy" if not view.stale else "stale"
    print(f"state={view.state} health={health} pid={view.pid or '-'} age={age} ticks={r.get('ticks','-')} host_samples={r.get('host_samples_persisted','-')} system_points={r.get('system_points_persisted','-')} dogstatsd_points={r.get('statsd_points_persisted','-')}")
    for key in ("last_host_storage_error","last_system_storage_error","last_statsd_storage_error","last_retention_error"):
        if r.get(key): print(f"{key}={r[key]}")
    return 0 if view.state=="running" and not view.stale else 3

def _runtime_for(store,args):
    statsd=None if args.no_dogstatsd else StatsDMetricsWorker(host=args.dogstatsd_host,port=args.dogstatsd_port,flush_interval_s=args.dogstatsd_flush_interval)
    return LocalAgentRuntime(store,statsd_worker=statsd)

def _run_agent(args):
    log=logging.getLogger("profiler.agent"); stop=threading.Event(); _install_signal_handlers(stop)
    database=Path(args.database).expanduser(); status_path=Path(args.status_file).expanduser(); started=time.time(); pid=os.getpid(); runtime=None; snapshot=None
    log.info("starting headless agent; database=%s dogstatsd=%s",database,"disabled" if args.no_dogstatsd else f"{args.dogstatsd_host}:{args.dogstatsd_port}")
    try:
        with SQLiteTelemetryStore(database) as store:
            runtime=_runtime_for(store,args); runtime.start(); snapshot=runtime.snapshot()
            try: write_agent_status(status_path,state="running",pid=pid,started_at=started,runtime_snapshot=snapshot,database_path=database)
            except Exception as exc: log.warning("could not write agent status: %s",exc)
            next_heartbeat=time.monotonic()+args.heartbeat_interval
            while not stop.wait(args.poll_interval):
                runtime.run_once(); now=time.monotonic()
                if now>=next_heartbeat:
                    snapshot=runtime.snapshot()
                    try: write_agent_status(status_path,state="running",pid=pid,started_at=started,runtime_snapshot=snapshot,database_path=database)
                    except Exception as exc: log.warning("could not update agent heartbeat: %s",exc)
                    next_heartbeat=now+args.heartbeat_interval
            runtime.stop(); snapshot=runtime.snapshot()
            try: write_agent_status(status_path,state="stopped",pid=pid,started_at=started,runtime_snapshot=snapshot,database_path=database)
            except Exception as exc: log.warning("could not write final agent status: %s",exc)
    except KeyboardInterrupt:
        stop.set()
        if runtime is not None: runtime.stop()
        return 130
    except Exception: log.exception("agent terminated unexpectedly"); return 1
    if snapshot is not None: log.info("agent stopped; ticks=%d host_samples=%d system_points=%d dogstatsd_points=%d",snapshot.ticks,snapshot.host_samples_persisted,snapshot.system_points_persisted,snapshot.statsd_points_persisted)
    return 0

def main(argv=None):
    p=build_parser(); args=p.parse_args(argv)
    if args.poll_interval<=0 or args.heartbeat_interval<=0 or args.status_max_age<=0 or args.dogstatsd_flush_interval<=0: p.error("intervals must be positive")
    if not 0<=args.dogstatsd_port<=65535: p.error("--dogstatsd-port must be in [0, 65535]")
    logging.basicConfig(level=getattr(logging,args.log_level),format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.status: return _print_status(Path(args.status_file).expanduser(),args.status_max_age)
    return _run_agent(args)
if __name__=="__main__": raise SystemExit(main())
