import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from profiler.core.agent_runtime import LocalAgentRuntime
from profiler.core.custom_metrics import CustomMetricPoint
from profiler.core.storage import SQLiteTelemetryStore

class EmptyHost:
    def start(self):pass
    def stop(self,timeout_s=2):return True
    def drain(self,limit):return ()
    def requeue_front(self,items):return 0
class EmptyMetrics:
    def start(self):pass
    def stop(self,timeout_s=2):return True
    def drain_points(self,limit):return ()
    def requeue_points(self,items):return 0
class FakeStatsD(EmptyMetrics):
    def __init__(self):self.items=[CustomMetricPoint(timestamp=1,name="jobs",value=4,tags=("service:worker",),metric_type="count",source="dogstatsd")];self.started=False;self.stopped=False
    def start(self):self.started=True;return ("127.0.0.1",8125)
    def stop(self,timeout_s=2):self.stopped=True;return True
    def drain_points(self,limit):out=self.items[:limit];del self.items[:limit];return tuple(out)
    def requeue_points(self,items):self.items[:0]=list(items);return len(items)
    def snapshot(self):return {"running":self.started and not self.stopped}

class Tests(unittest.TestCase):
    def test_statsd_points_are_durable_and_visible_in_snapshot(self):
        with SQLiteTelemetryStore() as store:
            statsd=FakeStatsD();runtime=LocalAgentRuntime(store,host_worker=EmptyHost(),system_worker=EmptyMetrics(),statsd_worker=statsd)
            runtime.start();runtime.run_once();snap=runtime.snapshot()
            self.assertEqual(snap.statsd_points_persisted,1);self.assertEqual(store.query_custom_metrics(source="dogstatsd",limit=10)[0]["name"],"jobs")
            self.assertTrue(runtime.stop());self.assertTrue(statsd.stopped)
    def test_statsd_is_optional(self):
        runtime=LocalAgentRuntime(type("S",(),{"prune_host_samples":lambda *a,**k:0,"prune_custom_metrics":lambda *a,**k:0})(),host_worker=EmptyHost(),system_worker=EmptyMetrics())
        self.assertIsNone(runtime.snapshot().statsd)
if __name__=="__main__":unittest.main()
