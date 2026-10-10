"""Unit tests for the audio pipeline / broadcaster (no network, no Kodi).

    python3 -m unittest discover -s tests -v
"""

import os
import struct
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service.sendspin", "resources", "lib"))

from sendspin.audio import AudioFormat, AudioPipeline, WavStreamServer, apply_gain, volume_to_gain  # noqa: E402
from sendspin.clock import now_us  # noqa: E402


class FakeClock:
    """Server clock == local clock."""

    synced = True

    def server_to_local(self, ts):
        return ts


FMT = AudioFormat(44100, 2, 16)
CHUNK_US = 25_000


def chunk(value=1000):
    n = FMT.bytes_for_us(CHUNK_US) // 2
    return struct.pack("<%dh" % n, *([value] * n))


class Feeder(threading.Thread):
    """Pushes chunks timestamped ~1 s ahead of now, like Music Assistant does."""

    def __init__(self, pipeline, seconds, value=1000):
        super().__init__(daemon=True)
        self.pipeline, self.seconds, self.value = pipeline, seconds, value

    def run(self):
        t = now_us() + 1_000_000
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            # stay ~1 s ahead of real time
            while t < now_us() + 1_000_000:
                self.pipeline.push(t, chunk(self.value))
                t += CHUNK_US
            time.sleep(0.01)


class Reader(threading.Thread):
    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.bytes = 0
        self.closed_at = None
        self.error = None

    def run(self):
        try:
            with urllib.request.urlopen(self.url, timeout=10) as r:
                r.read(44)
                while True:
                    b = r.read(4096)
                    if not b:
                        break
                    self.bytes += len(b)
        except Exception as exc:  # noqa: BLE001
            self.error = exc
        self.closed_at = time.monotonic()


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = AudioPipeline(FakeClock(), lead_ms=500)
        self.server = WavStreamServer(self.pipeline)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_gain(self):
        pcm = struct.pack("<4h", 1000, -1000, 32767, -32768)
        self.assertEqual(apply_gain(pcm, 2, 1.0), pcm)
        self.assertEqual(apply_gain(pcm, 2, 0.0), bytes(8))
        half = struct.unpack("<4h", apply_gain(pcm, 2, 0.5))
        self.assertEqual(half[0], 500)
        self.assertAlmostEqual(volume_to_gain(50, False), 0.25)
        self.assertEqual(volume_to_gain(80, True), 0.0)

    def test_realtime_rate_and_close_on_end(self):
        gen, _ = self.pipeline.start(FMT)
        Feeder(self.pipeline, 3).start()
        reader = Reader(self.server.url(gen))
        reader.start()
        time.sleep(3)
        self.pipeline.end()
        t_end = time.monotonic()
        reader.join(5)
        self.assertIsNotNone(reader.closed_at, "HTTP stream did not close after stream end")
        self.assertLess(reader.closed_at - t_end, 2.0)
        # first audio plays at +1.0 s and is released 0.5 s early; after 3 s we hold
        # audio up to +3.5 s => ~2.5 s. More would mean we dumped the buffer too early.
        seconds = reader.bytes / (44100 * 4)
        self.assertGreater(seconds, 2.3)
        self.assertLess(seconds, 3.0)

    def test_two_readers_both_get_audio(self):
        gen, _ = self.pipeline.start(FMT)
        Feeder(self.pipeline, 3).start()
        r1 = Reader(self.server.url(gen))
        r1.start()
        time.sleep(1)
        r2 = Reader(self.server.url(gen))  # e.g. Kodi's player opening after its probe
        r2.start()
        time.sleep(2)
        self.pipeline.end()
        r1.join(5)
        r2.join(5)
        self.assertGreater(r1.bytes, 44100 * 4 * 2)
        self.assertGreater(r2.bytes, 44100 * 4 * 1)

    def test_continuous_start_keeps_generation(self):
        gen, cont = self.pipeline.start(FMT)
        self.assertFalse(cont)
        Feeder(self.pipeline, 1.5).start()
        reader = Reader(self.server.url(gen))
        reader.start()
        time.sleep(1.5)
        gen2, cont2 = self.pipeline.start(FMT, continuous=True)
        self.assertTrue(cont2)
        self.assertEqual(gen, gen2)
        self.pipeline.end()
        reader.join(5)

    def test_ended_stream_returns_404(self):
        gen, _ = self.pipeline.start(FMT)
        self.pipeline.end()
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.server.url(gen), timeout=5)


if __name__ == "__main__":
    unittest.main()
