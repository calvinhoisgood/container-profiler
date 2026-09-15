import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profiler.core.log_stream import BoundedLogBuffer, DockerLogFollower, LogStreamDecoder
from profiler.core.workload import ContainerLogLine


class FakeStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class FakeContainer:
    def __init__(self, chunks):
        self.stream = FakeStream(chunks)
        self.calls = []

    def logs(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


class FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, container_id):
        if container_id != "abc":
            raise KeyError(container_id)
        return self.container


class FakeClient:
    def __init__(self, container):
        self.containers = FakeContainers(container)


class LogStreamTests(unittest.TestCase):
    def test_buffer_drops_oldest_by_line_limit(self):
        buffer = BoundedLogBuffer(max_lines=2, max_bytes=1000)
        buffer.extend([
            ContainerLogLine(None, "a"),
            ContainerLogLine(None, "b"),
            ContainerLogLine(None, "c"),
        ])
        self.assertEqual([line.message for line in buffer.drain(10)], ["b", "c"])
        stats = buffer.stats()
        self.assertEqual(stats.dropped, 1)
        self.assertEqual(stats.enqueued, 3)
        self.assertEqual(stats.drained, 2)

    def test_buffer_byte_limit_and_oversize_line_are_accounted(self):
        buffer = BoundedLogBuffer(max_lines=10, max_bytes=6)
        self.assertTrue(buffer.push(ContainerLogLine(None, "abc")))
        self.assertTrue(buffer.push(ContainerLogLine(None, "def")))
        self.assertEqual([line.message for line in buffer.drain(10)], ["def"])
        self.assertEqual(buffer.stats().dropped, 1)
        self.assertFalse(buffer.push(ContainerLogLine(None, "toolong")))
        self.assertEqual(buffer.stats().dropped, 2)

    def test_decoder_joins_split_chunks_and_parses_timestamp(self):
        decoder = LogStreamDecoder(max_pending_bytes=100)
        self.assertEqual(decoder.feed(b"2026-09-14T12:00:00Z hel"), ())
        rows = decoder.feed(b"lo\nplain\n")
        self.assertEqual([line.message for line in rows], ["hello", "plain"])
        self.assertEqual(rows[0].timestamp, "2026-09-14T12:00:00Z")

    def test_decoder_drops_pathological_unterminated_fragment(self):
        decoder = LogStreamDecoder(max_pending_bytes=4)
        self.assertEqual(decoder.feed(b"12345"), ())
        self.assertEqual(decoder.dropped_fragments, 1)

    def test_follower_requests_follow_stream_and_feeds_buffer(self):
        container = FakeContainer([
            b"2026-09-14T12:00:00Z one\n",
            b"two\n",
        ])
        buffer = BoundedLogBuffer(max_lines=10, max_bytes=1000)
        follower = DockerLogFollower(FakeClient(container), "abc", buffer, tail=12)
        follower.run()

        self.assertEqual([line.message for line in buffer.drain(10)], ["one", "two"])
        call = container.calls[0]
        self.assertTrue(call["follow"])
        self.assertTrue(call["stream"])
        self.assertTrue(call["timestamps"])
        self.assertEqual(call["tail"], 12)
        self.assertTrue(container.stream.closed)

    def test_stop_closes_active_stream(self):
        container = FakeContainer([])
        follower = DockerLogFollower(
            FakeClient(container), "abc", BoundedLogBuffer()
        )
        follower._stream = container.stream
        follower.stop()
        self.assertTrue(container.stream.closed)

    def test_stream_error_is_explicit(self):
        class BadContainer:
            def logs(self, **kwargs):
                raise RuntimeError("driver failed")

        follower = DockerLogFollower(
            FakeClient(BadContainer()), "abc", BoundedLogBuffer()
        )
        follower.run()
        self.assertEqual(follower.last_error, "driver failed")


if __name__ == "__main__":
    unittest.main()
