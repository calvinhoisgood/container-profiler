import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.alerting import AlertEvent, AlertStatus
from profiler.core.storage import SQLiteTelemetryStore


class SQLiteTelemetryStoreTests(unittest.TestCase):
    def test_round_trip_session_and_samples(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "telemetry.db"
            with SQLiteTelemetryStore(path) as store:
                store.start_session(
                    "s1", container_id="abc", started_at="2026-01-01T00:00:00+00:00",
                    target_interval_ms=1000, metadata={"image": "demo:1"},
                )
                count = store.append_samples("s1", [
                    {"timestamp": "t0", "elapsed_s": 0.0, "cpu_percent": 10, "gpu_power_w": None},
                    {"timestamp": "t1", "elapsed_s": 1.0, "cpu_percent": 20, "gpu_power_w": 0.0},
                    {"timestamp": "t2", "elapsed_s": 2.0, "cpu_percent": 30, "gpu_power_w": 120.0},
                ])
                self.assertEqual(count, 3)
                store.finish_session("s1", ended_at="2026-01-01T00:00:02+00:00")

                session = store.get_session("s1")
                self.assertEqual(session["metadata"]["image"], "demo:1")
                self.assertEqual(session["ended_at"], "2026-01-01T00:00:02+00:00")
                self.assertEqual(store.list_sessions()[0]["sample_count"], 3)

                rows = store.query_samples("s1", start_elapsed_s=1.0, end_elapsed_s=2.0)
                self.assertEqual([row["cpu_percent"] for row in rows], [20.0, 30.0])
                all_rows = store.query_samples("s1")
                self.assertIsNone(all_rows[0]["gpu_power_w"])
                self.assertEqual(all_rows[1]["gpu_power_w"], 0.0)

            with SQLiteTelemetryStore(path) as store:
                self.assertEqual(len(store.query_samples("s1")), 3)
                self.assertTrue(store.delete_session("s1"))
                self.assertEqual(store.query_samples("s1"), [])

    def test_finish_unknown_session_fails(self):
        with SQLiteTelemetryStore() as store:
            with self.assertRaises(KeyError):
                store.finish_session("missing")

    def test_alert_event_persistence_filters_and_acknowledgement(self):
        with SQLiteTelemetryStore() as store:
            store.start_session("s1", container_id="abc")
            firing = AlertEvent(
                timestamp=10.0, rule_name="cpu-hot", metric="cpu_percent",
                severity="critical", previous=AlertStatus.OK,
                current=AlertStatus.FIRING, value=95.0, message="cpu hot",
            )
            recovered = AlertEvent(
                timestamp=20.0, rule_name="cpu-hot", metric="cpu_percent",
                severity="critical", previous=AlertStatus.FIRING,
                current=AlertStatus.OK, value=50.0, message="cpu recovered",
            )
            firing_id = store.append_alert_event(firing, session_id="s1", container_id="abc")
            recovered_id = store.append_alert_event(recovered, session_id="s1", container_id="abc")

            rows = store.query_alert_events(container_id="abc")
            self.assertEqual([row["id"] for row in rows], [recovered_id, firing_id])
            self.assertEqual(store.query_alert_events(current_status="firing")[0]["value"], 95.0)
            self.assertEqual(store.query_alert_events(severity="critical", rule_name="cpu-hot")[0]["id"], recovered_id)
            self.assertEqual(len(store.query_alert_events(acknowledged=False)), 2)

            self.assertTrue(store.acknowledge_alert_event(
                firing_id, acknowledged_by="operator", note="investigating",
                acknowledged_at="2026-09-15T01:00:00+00:00",
            ))
            self.assertFalse(store.acknowledge_alert_event(firing_id))
            acked = store.query_alert_events(acknowledged=True)
            self.assertEqual(acked[0]["acknowledged_by"], "operator")
            self.assertEqual(acked[0]["note"], "investigating")
            self.assertEqual(len(store.query_alert_events(acknowledged=False)), 1)

    def test_deleting_session_keeps_alert_audit_record(self):
        with SQLiteTelemetryStore() as store:
            store.start_session("s1", container_id="abc")
            event = AlertEvent(
                timestamp=10.0, rule_name="memory", metric="memory_percent",
                severity="warning", previous=AlertStatus.OK,
                current=AlertStatus.FIRING, value=90.0, message="memory high",
            )
            event_id = store.append_alert_event(event, session_id="s1", container_id="abc")
            self.assertTrue(store.delete_session("s1"))
            rows = store.query_alert_events()
            self.assertEqual(rows[0]["id"], event_id)
            self.assertIsNone(rows[0]["session_id"])
            self.assertEqual(rows[0]["container_id"], "abc")


class DataManagerPersistenceTests(unittest.TestCase):
    def test_data_manager_batches_and_finishes_session(self):
        from profiler.core.data_manager import DataManager
        from profiler.core.models import ContainerStats, PowerStats

        store = SQLiteTelemetryStore()
        manager = DataManager(store, persist_batch_size=2)
        manager.start_recording("abc", 1000)
        sid = manager.session_id
        self.assertIsNotNone(sid)
        for i in range(3):
            manager.add_record(
                "abc",
                ContainerStats(1000+i, 10+i, 100, 200, 50, i, i, None, None, 2),
                PowerStats(cpu_power_w=20+i),
            )
        self.assertEqual(len(store.query_samples(sid)), 2)
        manager.stop_recording()
        self.assertEqual(len(store.query_samples(sid)), 3)
        self.assertIsNotNone(store.get_session(sid)["ended_at"])
        manager.close()


if __name__ == "__main__":
    unittest.main()
