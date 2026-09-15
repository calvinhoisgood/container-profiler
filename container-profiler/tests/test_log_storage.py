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
            percent = repo.query(contains="%")
            underscore = repo.query(contains="_")
            self.assertEqual([item["message"] for item in percent], ["100% ready"])
            self.assertEqual([item["message"] for item in underscore], ["under_score"])
            web = repo.query(container_id="a", start_timestamp=1.5, limit=10)
            self.assertEqual([item["message"] for item in web], ["under_score"])
            self.assertEqual(repo.query(container_name="web", contains="ready")[0]["tags"], ("service:web",))
            first = repo.query(limit=1)[0]
            second_page = repo.query(
                before_timestamp=first["timestamp"], before_id=first["id"], limit=10
            )
            self.assertEqual([item["message"] for item in second_page], ["under_score", "100% ready"])

    def test_keyset_pagination_handles_out_of_order_inserts_and_equal_timestamps(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store)
            repo.append([
                ContainerLogRecord(20.0, "a", "newer-inserted-first"),
                ContainerLogRecord(10.0, "a", "older-inserted-second"),
                ContainerLogRecord(20.0, "a", "newer-same-time-later-id"),
            ])
            first = repo.query(limit=1)[0]
            self.assertEqual(first["message"], "newer-same-time-later-id")
            rest = repo.query(
                before_timestamp=first["timestamp"], before_id=first["id"], limit=10
            )
            self.assertEqual(
                [row["message"] for row in rest],
                ["newer-inserted-first", "older-inserted-second"],
            )
            with self.assertRaises(ValueError):
                repo.query(before_id=first["id"])

    def test_prune_age_and_row_bound(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store)
            repo.append(ContainerLogRecord(float(i), "a", f"line-{i}") for i in range(1, 6))
            self.assertEqual(repo.prune(before_timestamp=3.0), 2)
            self.assertEqual(repo.prune(max_rows=2), 1)
            self.assertEqual([row["message"] for row in repo.query(limit=10)], ["line-5", "line-4"])
            self.assertEqual(repo.prune(max_rows=2), 0)

    def test_invalid_container_identity_rejected(self):
        with SQLiteTelemetryStore() as store:
            repo = ContainerLogRepository(store)
            with self.assertRaises(ValueError):
                repo.append([ContainerLogRecord(1.0, "", "bad")])


if __name__ == "__main__":
    unittest.main()
