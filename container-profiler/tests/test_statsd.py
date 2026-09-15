import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.statsd import (
    MAX_LINE_CHARS,
    MAX_TAGS_PER_METRIC,
    StatsDAggregator,
    StatsDParseError,
    parse_statsd_line,
)


class StatsDParserTests(unittest.TestCase):
    def test_parse_tags_and_sample_rate(self):
        metric = parse_statsd_line(
            "requests:2|c|@0.5|#service:api,env:dev,service:api"
        )
        self.assertEqual(metric.name, "requests")
        self.assertEqual(metric.metric_type, "count")
        self.assertEqual(metric.sample_rate, 0.5)
        self.assertEqual(metric.tags, ("env:dev", "service:api"))

    def test_reject_non_finite_and_bad_extension(self):
        with self.assertRaises(StatsDParseError):
            parse_statsd_line("x:nan|g")
        with self.assertRaises(StatsDParseError):
            parse_statsd_line("x:1|g|foo")

    def test_parser_bounds_line_and_tags(self):
        with self.assertRaisesRegex(StatsDParseError, "line exceeds"):
            parse_statsd_line("x:" + "1" * MAX_LINE_CHARS + "|g")
        tags = ",".join(f"tag{i}:v" for i in range(MAX_TAGS_PER_METRIC + 1))
        with self.assertRaisesRegex(StatsDParseError, "more than"):
            parse_statsd_line(f"x:1|g|#{tags}")


class StatsDAggregatorTests(unittest.TestCase):
    def test_gauge_count_set_and_histogram(self):
        agg = StatsDAggregator(clock=lambda: 42.0)
        for line in [
            "depth:1|g|#q:a",
            "depth:3|g|#q:a",
            "requests:1|c|@0.5",
            "requests:2|c",
            "users:a|s",
            "users:a|s",
            "users:b|s",
            "latency:10|ms",
            "latency:20|ms",
            "latency:30|ms",
        ]:
            self.assertTrue(agg.ingest(line))
        flushed = {(m.name, m.metric_type): m for m in agg.flush()}
        self.assertEqual(flushed[("depth", "gauge")].values["value"], 3.0)
        self.assertEqual(flushed[("requests", "count")].values["value"], 4.0)
        self.assertEqual(flushed[("users", "set")].values["count"], 2.0)
        h = flushed[("latency", "histogram")]
        self.assertEqual(h.values["count"], 3.0)
        self.assertEqual(h.values["mean"], 20.0)
        self.assertEqual(h.values["p95"], 29.0)
        self.assertEqual(h.timestamp, 42.0)
        self.assertEqual(agg.pending_series, 0)

    def test_bad_packets_are_counted_not_raised(self):
        agg = StatsDAggregator()
        self.assertFalse(agg.ingest("not a metric"))
        self.assertEqual(agg.parse_errors, 1)
        self.assertEqual(agg.received, 0)

    def test_series_limit_rejects_new_cardinality_but_updates_existing(self):
        agg = StatsDAggregator(max_series=2)
        self.assertTrue(agg.ingest("a:1|c"))
        self.assertTrue(agg.ingest("b:1|g"))
        self.assertFalse(agg.ingest("c:1|c"))
        self.assertTrue(agg.ingest("a:2|c"))
        self.assertEqual(agg.pending_series, 2)
        self.assertEqual(agg.dropped_series, 1)
        flushed = {m.name: m for m in agg.flush()}
        self.assertEqual(flushed["a"].values["value"], 3.0)
        self.assertNotIn("c", flushed)

    def test_histogram_and_set_values_are_bounded(self):
        agg = StatsDAggregator(max_histogram_values=2, max_set_values=2)
        self.assertTrue(agg.ingest("latency:1|h"))
        self.assertTrue(agg.ingest("latency:2|h"))
        self.assertFalse(agg.ingest("latency:3|h"))
        self.assertTrue(agg.ingest("users:a|s"))
        self.assertTrue(agg.ingest("users:b|s"))
        self.assertTrue(agg.ingest("users:a|s"))
        self.assertFalse(agg.ingest("users:c|s"))
        self.assertEqual(agg.dropped_histogram_values, 1)
        self.assertEqual(agg.dropped_set_values, 1)
        self.assertEqual(agg.dropped_values, 2)
        flushed = {m.name: m for m in agg.flush()}
        self.assertEqual(flushed["latency"].values["count"], 2.0)
        self.assertEqual(flushed["users"].values["count"], 2.0)
        self.assertEqual(agg.dropped_values, 2)


if __name__ == "__main__":
    unittest.main()
