"""Headless end-to-end test of the Sendspin core (no Kodi, no speakers).

Connects to a real Sendspin server as a player, simulates Kodi by reading the
local WAV stream over HTTP, and reports timing + audio statistics. Audio is
written to a file only - nothing is played out loud.

    python3 tests/harness.py ws://192.168.1.10:8927/sendspin 60 /tmp/sendspin_test.wav
"""

import logging
import math
import os
import struct
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service.sendspin", "resources", "lib"))

from sendspin.audio import AudioPipeline, WavStreamServer  # noqa: E402
from sendspin.client import Listener, SendspinClient  # noqa: E402
from sendspin.clock import ClockSync, now_us  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://192.168.1.10:8927/sendspin"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 60
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/sendspin_test.wav"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("harness")


class FakeKodi(Listener):
    """Plays the role of Kodi's player: GETs the WAV URL and consumes it."""

    def __init__(self, server, pipeline):
        self.server = server
        self.pipeline = pipeline
        self.reads = []  # (local_us, nbytes)
        self.pcm = bytearray()
        self.header = None
        self.events = []

    def on_connected(self, server_name):
        self.events.append(("connected", server_name))

    def on_stream_start(self, fmt, generation):
        self.events.append(("stream_start", repr(fmt), generation))
        threading.Thread(target=self._consume, args=(generation,), daemon=True).start()

    def on_stream_clear(self, generation):
        self.events.append(("stream_clear", generation))
        threading.Thread(target=self._consume, args=(generation,), daemon=True).start()

    def on_stream_end(self):
        self.events.append(("stream_end",))

    def on_metadata(self, md):
        self.events.append(("metadata", md.get("title"), md.get("artist"), md.get("artwork_url")))

    def on_volume(self, volume, muted):
        self.events.append(("volume", volume, muted))

    def _consume(self, generation):
        url = self.server.url(generation)
        log.info("fake kodi GET %s", url)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                self.header = r.read(44)
                while True:
                    b = r.read(4096)
                    if not b:
                        break
                    self.reads.append((now_us(), len(b)))
                    if len(self.pcm) < 48000 * 4 * 20:
                        self.pcm += b
        except Exception as exc:  # noqa: BLE001
            log.info("fake kodi stream closed: %s", exc)


def rms16(pcm):
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


def main():
    clock = ClockSync()
    pipeline = AudioPipeline(clock, lead_ms=800)
    server = WavStreamServer(pipeline)
    server.start()
    kodi = FakeKodi(server, pipeline)
    client = SendspinClient(URL, "kodi-sendspin-dev", "Kodi Sendspin (dev)", pipeline, kodi)
    client.start()
    t0 = time.time()
    while time.time() - t0 < DURATION:
        time.sleep(5)
        log.info("synced=%s rtt=%sus buffered=%.1fs stats=%s", clock.synced, clock.rtt_us,
                 pipeline.buffered_us() / 1e6, pipeline.stats)
    client.stop()
    server.stop()

    print("\n=== EVENTS ===")
    for e in kodi.events[:20]:
        print(" ", e)
    print("=== STATS ===", pipeline.stats)
    if kodi.reads:
        first, last = kodi.reads[0][0], kodi.reads[-1][0]
        total = sum(n for _, n in kodi.reads)
        span = (last - first) / 1e6
        fmt = pipeline.fmt
        rate = fmt.sample_rate * fmt.frame_bytes if fmt else 176400
        print("received %d bytes over %.1fs -> %.3fx real-time" % (total, span, (total / rate) / span if span else 0))
        print("WAV header ok:", kodi.header[:4] == b"RIFF" and kodi.header[8:12] == b"WAVE")
        print("RMS of first ~20s (16-bit): %.0f  (0 = silence)" % rms16(bytes(kodi.pcm)))
        if fmt:
            with open(OUT, "wb") as f:
                f.write(fmt.wav_header()[:40] + struct.pack("<I", len(kodi.pcm)) + bytes(kodi.pcm))
                f.seek(4)
                f.write(struct.pack("<I", 36 + len(kodi.pcm)))
            print("wrote", OUT)
    else:
        print("no audio consumed")


if __name__ == "__main__":
    main()
