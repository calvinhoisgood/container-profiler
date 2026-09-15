from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.log_storage import ContainerLogRecord, ContainerLogRepository
from profiler.core.storage import SQLiteTelemetryStore


class ContainerLogRepositoryTests(unittest.TestCase):
    def test_persist_query_literal_search_and_pagination(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store)
            self.assertEqual(repo.append([
                ContainerLogRecord(1.0, "a", "100% ready", "2026-09-15T10:00:00Z", "web", ("service:web",)),
                ContainerLogRecord(2.0, "a", "under_score", "2026-09-15T10:00:01Z", "web"),
                ContainerLogRecord(3.0, "b", "other", "2026-09-15T10:00:02Z", "db"),
            ]), 3)
            self.assertEqual([item["message"] for item in repo.query(contains="%")], ["100% ready"])
            self.assertEqual([item["message"] for item in repo.query(contains="_")], ["under_score"])
            self.assertEqual([item["message"] for item in repo.query(container_id="a", start_timestamp=1.5)], ["under_score"])
            first = repo.query(limit=1)[0]
            second_page = repo.query(before_timestamp=first["timestamp"], before_id=first["id"], limit=10)
            self.assertEqual([item["message"] for item in second_page], ["under_score", "100% ready"])

    def test_keyset_pagination_handles_out_of_order_inserts_and_equal_timestamps(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store); repo.append([ContainerLogRecord(20.0, "a", "newer-inserted-first"), ContainerLogRecord(10.0, "a", "older-inserted-second"), ContainerLogRecord(20.0, "a", "newer-same-time-later-id")])
            first = repo.query(limit=1)[0]; self.assertEqual(first["message"], "newer-same-time-later-id")
            rest = repo.query(before_timestamp=first["timestamp"], before_id=first["id"], limit=10)
            self.assertEqual([row["message"] for row in rest], ["newer-inserted-first", "older-inserted-second"])
            with self.assertRaises(ValueError): repo.query(before_id=first["id"])

    def test_replay_state_tracks_only_durable_timestamped_rows_and_multiplicity(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store); base = 1789466400.0; repeated = ContainerLogRecord(base + 20, "a", "same", "2026-09-15T10:00:20Z")
            repo.append([repeated, repeated, ContainerLogRecord(base + 21, "a", "new", "2026-09-15T10:00:21Z"), ContainerLogRecord(base + 99, "a", "untimestamped"), ContainerLogRecord(base + 100, "a", "bad-ts", "not-a-timestamp")])
            repo.append([ContainerLogRecord(base + 19, "a", "older", "2026-09-15T10:00:19Z")])
            cursors, recent = repo.replay_state(max_entries_per_container=10)
            self.assertEqual(cursors["a"], base + 21); self.assertEqual(recent["a"].count(("2026-09-15T10:00:20Z", "same")), 2); self.assertIn(("2026-09-15T10:00:21Z", "new"), recent["a"]); self.assertNotIn(("not-a-timestamp", "bad-ts"), recent["a"])

    def test_prune_age_and_row_bound(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store); repo.append(ContainerLogRecord(float(i), "a", f"line-{i}") for i in range(1, 6)); self.assertEqual(repo.prune(before_timestamp=3.0), 2); self.assertEqual(repo.prune(max_rows=2), 1); self.assertEqual([row["message"] for row in repo.query(limit=10)], ["line-5", "line-4"]); self.assertEqual(repo.prune(max_rows=2), 0)

    def test_additive_migration_upgrades_earlier_log_table(self):
        with SQLiteTelemetryStore() as store:
            store._db.execute("CREATE TABLE container_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL NOT NULL,docker_timestamp TEXT,container_id TEXT NOT NULL,container_name TEXT,message TEXT NOT NULL,message_bytes INTEGER NOT NULL,tags_json TEXT NOT NULL DEFAULT '[]')"); store._db.commit(); ContainerLogRepository(store)
            self.assertIn("docker_epoch", {row["name"] for row in store._db.execute("PRAGMA table_info(container_logs)")})

    def test_invalid_container_identity_rejected(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store)
            with self.assertRaises(ValueError): repo.append([ContainerLogRecord(1.0, "", "bad")])


if __name__ == "__main__": unittest.main()
