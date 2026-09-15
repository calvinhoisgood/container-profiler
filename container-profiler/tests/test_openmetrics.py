from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.openmetrics import OpenMetricsError, parse_openmetrics_text


PAYLOAD = '''# HELP http_requests_total Requests
# TYPE http_requests_total counter
# UNIT temperature_celsius celsius
http_requests_total{method="GET",route="/a"} 12 1700000000000
temperature_celsius 42.5
untyped 7
# EOF
'''


class OpenMetricsTests(unittest.TestCase):
    def test_metadata_labels_namespace_and_filtering(self):
        samples = parse_openmetrics_text(
            PAYLOAD,
            metric_patterns=(r"http_.*", r"temperature_.*"),
            namespace="app",
        )
        self.assertEqual([sample.name for sample in samples], [
            "app.http_requests_total", "app.temperature_celsius"
        ])
        self.assertEqual(samples[0].metric_type, "counter")
        self.assertEqual(samples[0].timestamp_ms, 1700000000000)
        self.assertEqual(samples[0].tags, ("method:GET", "route:/a"))
        self.assertEqual(samples[1].unit, "celsius")

    def test_require_type_skips_untyped_metrics(self):
        samples = parse_openmetrics_text(PAYLOAD, require_type=True)
        self.assertEqual([sample.name for sample in samples], ["http_requests_total"])

    def test_label_escapes_and_canonical_order(self):
        sample = parse_openmetrics_text(
            'metric{z="quote\\\"",a="hello\\nworld"} 1\n'
        )[0]
        self.assertEqual(sample.labels, (("a", "hello\nworld"), ("z", 'quote"')))

    def test_exemplar_is_ignored_without_losing_sample(self):
        sample = parse_openmetrics_text(
            'request_duration_seconds 2 # {trace_id="abc"} 1.0\n'
        )[0]
        self.assertEqual(sample.value, 2.0)

    def test_metric_limit_is_enforced(self):
        with self.assertRaisesRegex(OpenMetricsError, "metric limit exceeded"):
            parse_openmetrics_text("a 1\nb 2\n", max_samples=1)

    def test_label_limit_is_enforced(self):
        with self.assertRaisesRegex(OpenMetricsError, "too many labels"):
            parse_openmetrics_text('a{x="1",y="2"} 1\n', max_labels_per_sample=1)

    def test_non_finite_values_are_rejected(self):
        for value in ("NaN", "+Inf", "-Inf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(OpenMetricsError, "non-finite"):
                    parse_openmetrics_text(f"a {value}\n")

    def test_malformed_payload_is_explicit(self):
        with self.assertRaisesRegex(OpenMetricsError, "line 1"):
            parse_openmetrics_text("this is not a metric\n")

    def test_empty_allowlist_is_rejected(self):
        with self.assertRaisesRegex(OpenMetricsError, "at least one"):
            parse_openmetrics_text("a 1\n", metric_patterns=())

    def test_invalid_regex_is_rejected(self):
        with self.assertRaisesRegex(OpenMetricsError, "invalid metric regex"):
            parse_openmetrics_text("a 1\n", metric_patterns=("[",))


if __name__ == "__main__":
    unittest.main()
