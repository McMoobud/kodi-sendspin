"""Timestamped PCM -> continuous WAV stream for Kodi's player.

Sendspin delivers PCM chunks stamped with the server time at which they must
be heard. Kodi can only play a URL, so we expose an endless WAV stream on
localhost and *release* each chunk ``lead`` microseconds before its play time.
Kodi keeps roughly ``lead`` worth of audio in its pipeline, so what comes out
of the speakers is aligned with the server timeline (fine-tuned with the
static delay, which the user / Music Assistant can adjust).

The writer keeps a continuous timeline: gaps are filled with silence, overlaps
are trimmed and audio that is already too late is dropped, so Kodi never
underruns and never drifts behind.
"""

import array
from collections import deque
import logging
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .clock import now_us

try:  # audioop is C-fast; removed in Python 3.13 but present in Kodi's Python
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - depends on interpreter
    audioop = None

log = logging.getLogger("sendspin.audio")

LATE_DROP_US = 150_000  # drop audio that would play this late
GAP_FILL_US = 2_000  # insert silence for gaps larger than this
STARVE_FILL_US = 120_000  # keep Kodi fed with silence when the network stalls


class AudioFormat:
    def __init__(self, sample_rate, channels, bit_depth):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.bit_depth = int(bit_depth)

    @property
    def frame_bytes(self):
        return self.channels * self.bit_depth // 8

    @property
    def bytes_per_us(self):
        return self.sample_rate * self.frame_bytes / 1_000_000.0

    def duration_us(self, nbytes):
        return int(nbytes / self.bytes_per_us)

    def bytes_for_us(self, us):
        frames = int(us * self.sample_rate / 1_000_000)
        return max(frames, 0) * self.frame_bytes

    def wav_header(self):
        byte_rate = self.sample_rate * self.frame_bytes
        data_size = 0xFFFFFFFF - 36  # "infinite" stream
        return (
            b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, self.channels, self.sample_rate,
                                    byte_rate, self.frame_bytes, self.bit_depth)
            + b"data" + struct.pack("<I", data_size)
        )

    def __eq__(self, other):
        return isinstance(other, AudioFormat) and (
            self.sample_rate, self.channels, self.bit_depth
        ) == (other.sample_rate, other.channels, other.bit_depth)

    def __repr__(self):
        return "%dHz/%dch/%dbit" % (self.sample_rate, self.channels, self.bit_depth)


def apply_gain(pcm, width, gain):
    """Scale 16-bit (or 24/32) little-endian PCM by ``gain`` (0..1)."""
    if gain >= 0.999:
        return pcm
    if gain <= 0.0:
        return bytes(len(pcm))
    if audioop is not None:
        return audioop.mul(pcm, width, gain)
    if width != 2:  # pragma: no cover - fallback only handles 16-bit
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":  # pragma: no cover
        samples.byteswap()
    scaled = array.array("h", (int(s * gain) for s in samples))
    if sys.byteorder != "little":  # pragma: no cover
        scaled.byteswap()
    return scaled.tobytes()


def volume_to_gain(volume, muted):
    """Sendspin volume 0..100 -> perceptual gain (squared curve)."""
    if muted:
        return 0.0
    v = max(0, min(100, int(volume))) / 100.0
    return v * v


class AudioPipeline:
    """Holds timestamped chunks and hands out time-aligned PCM to the HTTP writer."""

    def __init__(self, clock, lead_ms=800, static_delay_ms=0):
        self._clock = clock
        self._cond = threading.Condition()
        self._chunks = deque()  # [server_ts_us, pcm]
        self.fmt = None
        self.generation = 0
        self.active = False
        self.lead_us = int(lead_ms * 1000)
        self.static_delay_us = int(static_delay_ms * 1000)
        self.volume = 100
        self.muted = False
        self._cursor = None  # local µs up to which audio has been written
        self.reader = 0  # only the newest HTTP reader receives audio
        self.stats = {"received": 0, "written": 0, "dropped_late": 0, "silence_us": 0, "trimmed": 0}

    # -- control (protocol thread) ----------------------------------------
    def start(self, fmt):
        with self._cond:
            self.fmt = fmt
            self._chunks = deque()
            self._cursor = None
            self.generation += 1
            self.active = True
            self._cond.notify_all()
        log.info("stream start %r (generation %d)", fmt, self.generation)
        return self.generation

    def clear(self):
        """Drop queued audio (seek / skip). Kodi's own buffer is flushed by the caller."""
        with self._cond:
            self._chunks = deque()
            self._cursor = None
            self.generation += 1
            self._cond.notify_all()
        log.info("stream clear (generation %d)", self.generation)
        return self.generation

    def end(self):
        with self._cond:
            self.active = False
            self._chunks = deque()
            self._cursor = None
            self._cond.notify_all()
        log.info("stream end")

    def push(self, server_ts, pcm):
        with self._cond:
            if not self.active:
                return
            self._chunks.append([server_ts, pcm])
            self.stats["received"] += 1
            self._cond.notify_all()

    def buffered_us(self):
        with self._cond:
            if self.fmt is None:
                return 0
            return sum(self.fmt.duration_us(len(c[1])) for c in self._chunks)

    # -- output (HTTP writer thread) --------------------------------------
    def _local_play_time(self, server_ts):
        local = self._clock.server_to_local(server_ts)
        if local is None:
            return None
        return local + self.static_delay_us

    def claim_reader(self):
        with self._cond:
            self.reader += 1
            self._cond.notify_all()
            return self.reader

    def next_block(self, generation, reader=None, timeout=0.5):
        """Block until audio for ``generation`` is due; return PCM bytes, b"" or None.

        ``None`` means the generation is over (stream ended / cleared / format
        changed) and the HTTP response should be closed.
        """
        with self._cond:
            while True:
                if generation != self.generation or not self.active:
                    return None
                if reader is not None and reader != self.reader:
                    return None  # a newer HTTP connection took over
                fmt = self.fmt
                now = now_us()
                release_horizon = now + self.lead_us
                if self._chunks:
                    ts, pcm = self._chunks[0]
                    start = self._local_play_time(ts)
                    if start is None:
                        self._cond.wait(0.05)
                        continue
                    dur = fmt.duration_us(len(pcm))
                    if start + dur < now - LATE_DROP_US:
                        self._chunks.popleft()
                        self.stats["dropped_late"] += 1
                        continue
                    if start <= release_horizon:
                        self._chunks.popleft()
                        return self._emit(fmt, start, dur, pcm)
                    # Not due yet: maybe we must fill silence so Kodi keeps playing.
                    fill = self._starvation_fill(fmt, now, release_horizon)
                    if fill:
                        return fill
                    wait = (start - release_horizon) / 1_000_000.0
                    self._cond.wait(min(max(wait, 0.002), timeout))
                    continue
                fill = self._starvation_fill(fmt, now, release_horizon)
                if fill:
                    return fill
                self._cond.wait(0.02)

    def _starvation_fill(self, fmt, now, release_horizon):
        """Write silence if Kodi would otherwise run dry (network hiccup)."""
        if self._cursor is None:
            return b""
        if self._cursor >= now + STARVE_FILL_US:
            return b""
        target = min(release_horizon, now + STARVE_FILL_US * 2)
        n = fmt.bytes_for_us(target - self._cursor)
        if n <= 0:
            return b""
        self.stats["silence_us"] += fmt.duration_us(n)
        self._cursor += fmt.duration_us(n)
        return bytes(n)

    def _emit(self, fmt, start, dur, pcm):
        out = []
        if self._cursor is not None:
            if start + dur <= self._cursor:  # fully covered by silence fill
                self.stats["trimmed"] += 1
                return b""
            if start < self._cursor - GAP_FILL_US:  # real overlap: trim the front
                cut = fmt.bytes_for_us(self._cursor - start)
                pcm = pcm[cut:]
                start = self._cursor
                self.stats["trimmed"] += 1
            elif start > self._cursor + GAP_FILL_US:  # gap: pad silence
                gap = fmt.bytes_for_us(start - self._cursor)
                out.append(bytes(gap))
                self.stats["silence_us"] += start - self._cursor
        # Sub-ms rounding jitter between chunks is absorbed: the cursor only moves forward.
        self._cursor = max(self._cursor or 0, start) + fmt.duration_us(len(pcm))
        gain = volume_to_gain(self.volume, self.muted)
        out.append(apply_gain(pcm, fmt.bit_depth // 8, gain))
        self.stats["written"] += 1
        return b"".join(out)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "kodi-sendspin"

    def log_message(self, fmt, *args):  # route to our logger, quietly
        log.debug("http: " + fmt, *args)

    def _parse_generation(self):
        # /stream/<generation>.wav
        try:
            return int(self.path.rsplit("/", 1)[-1].split(".")[0])
        except ValueError:
            return None

    def do_HEAD(self):  # noqa: N802
        pipeline = self.server.pipeline
        if pipeline.fmt is None:
            self.send_error(404)
            return
        self._headers()

    def _headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        pipeline = self.server.pipeline
        generation = self._parse_generation()
        if generation is None or generation != pipeline.generation or pipeline.fmt is None:
            self.send_error(404)
            return
        self._headers()
        fmt = pipeline.fmt
        reader = pipeline.claim_reader()
        try:
            self.wfile.write(fmt.wav_header())
            while not self.server.stopping:
                block = pipeline.next_block(generation, reader)
                if block is None:
                    break
                if block:
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            log.debug("http client went away")


class WavStreamServer:
    """Serves the pipeline as ``http://127.0.0.1:<port>/stream/<generation>.wav``."""

    def __init__(self, pipeline, host="127.0.0.1", port=0):
        self.pipeline = pipeline
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.pipeline = pipeline
        self._httpd.stopping = False
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="sendspin-http", daemon=True)

    @property
    def port(self):
        return self._httpd.server_address[1]

    def url(self, generation):
        return "http://127.0.0.1:%d/stream/%d.wav" % (self.port, generation)

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.stopping = True
        self._httpd.shutdown()
        self._httpd.server_close()
