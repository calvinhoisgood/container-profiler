import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.statsd import StatsDAggregator, StatsDParseError, parse_statsd_line


class StatsDParserTests(unittest.TestCase):
    def test_parse_tags_and_sample_rate(self):
        metric = parse_statsd_line("requests:2|c|@0.5|#service:api,env:dev,service:api")
        self.assertEqual(metric.name, "requests")
        self.assertEqual(metric.metric_type, "count")
        self.assertEqual(metric.sample_rate, 0.5)
        self.assertEqual(metric.tags, ("env:dev", "service:api"))

    def test_reject_non_finite_and_bad_extension(self):
        with self.assertRaises(StatsDParseError):
            parse_statsd_line("x:nan|g")
        with self.assertRaises(StatsDParseError):
            parse_statsd_line("x:1|g|foo")


class StatsDAggregatorTests(unittest.TestCase):
    def test_gauge_count_set_and_histogram(self):
        agg = StatsDAggregator(clock=lambda: 42.0)
        for line in [
            "depth:1|g|#q:a", "depth:3|g|#q:a",
            "requests:1|c|@0.5", "requests:2|c",
            "users:a|s", "users:a|s", "users:b|s",
            "latency:10|ms", "latency:20|ms", "latency:30|ms",
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


if __name__ == "__main__":
    unittest.main()
