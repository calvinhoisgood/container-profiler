import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.statsd_server import StatsDUDPServer


class StatsDUDPServerTests(unittest.TestCase):
    def test_udp_ingest_multiline_and_periodic_flush(self):
        received = []
        flushed = threading.Event()

        def on_flush(metrics):
            received.extend(metrics)
            flushed.set()

        server = StatsDUDPServer(port=0, flush_interval_s=0.05, on_flush=on_flush)
        address = server.start()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"requests:1|c\nrequests:2|c\ndepth:7|g|#queue:a", address)
            sock.close()
            self.assertTrue(flushed.wait(1.0))
        finally:
            self.assertTrue(server.stop())

        values = {(metric.name, metric.metric_type): metric.values for metric in received}
        self.assertEqual(values[("requests", "count")]["value"], 3.0)
        self.assertEqual(values[("depth", "gauge")]["value"], 7.0)
        self.assertEqual(server.datagrams_received, 1)
        self.assertEqual(server.lines_received, 3)
        self.assertGreaterEqual(server.flush_count, 1)

    def test_invalid_utf8_isolated_from_listener(self):
        server = StatsDUDPServer(port=0, flush_interval_s=0.05)
        address = server.start()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"\xff\xfe", address)
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"ok:1|c", address)
            sock.close()
            threading.Event().wait(0.1)
            self.assertTrue(server.is_running)
            self.assertEqual(server.decode_errors, 1)
            self.assertEqual(server.aggregator.received, 1)
        finally:
            self.assertTrue(server.stop())


if __name__ == "__main__":
    unittest.main()
