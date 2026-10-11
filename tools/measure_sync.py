"""Measure Kodi's real output timing against the Sendspin timeline (Linux test box).

Runs next to Kodi on a machine whose PulseAudio default sink is a *null sink*
(silent). Records the sink monitor with per-chunk CLOCK_MONOTONIC timestamps,
then cross-correlates the recording with the audio the add-on released
(SENDSPIN_TIMELINE_DIR dump) to find when each sample *actually* left Kodi
versus when the server wanted it heard.

    python3 measure_sync.py /tmp/tl 20      # record 20 s while music is playing

Positive error = Kodi plays LATE; use the add-on's sync trim (static delay) or
output-latency setting to compensate on real hardware.
"""

import glob
import os
import subprocess
import sys
import time

import numpy as np

TL_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tl"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 20
RATE, CH = 44100, 2
FRAME = CH * 2


def record(seconds):
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/0")
    p = subprocess.Popen(["parec", "-d", "silent.monitor", "--format=s16le", "--rate=%d" % RATE,
                          "--channels=%d" % CH, "--latency-msec=5", "--raw"],
                         stdout=subprocess.PIPE, env=env)
    chunks = []  # (monotonic_us at read completion, bytes)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        b = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        chunks.append((time.monotonic_ns() // 1000, b))
    p.terminate()
    return chunks


def mono(raw):
    a = np.frombuffer(raw[: len(raw) // FRAME * FRAME], dtype="<i2").astype(np.float32)
    return a.reshape(-1, CH).mean(axis=1)


def main():
    raws = sorted(glob.glob(os.path.join(TL_DIR, "timeline-*.raw")), key=os.path.getmtime)
    if not raws:
        sys.exit("no timeline dump in %s - start Kodi with SENDSPIN_TIMELINE_DIR set" % TL_DIR)
    print("recording %.0f s from silent.monitor ..." % SECONDS)
    chunks = record(SECONDS)
    raw_path = raws[-1]
    index = [tuple(map(int, l.split())) for l in open(raw_path[:-4] + ".jsonl").read().splitlines()[1:]]
    # only load the released audio whose intended play time is near the recording window
    t_lo, t_hi = chunks[0][0] - 8_000_000, chunks[-1][0] + 8_000_000
    sel = [(off, ln, t) for off, ln, t in index if t and t_lo <= t <= t_hi]
    if not sel:
        sys.exit("recording window is outside the dumped timeline (raise SENDSPIN_TIMELINE_SECONDS)")
    base_off = sel[0][0]
    with open(raw_path, "rb") as f:
        f.seek(base_off)
        released = f.read(sel[-1][0] + sel[-1][1] - base_off)
    index = [(off - base_off, ln, t) for off, ln, t in sel]
    cap = b"".join(b for _, b in chunks)
    # sample index -> capture time: the last sample of each chunk was captured at its timestamp
    ends, total = [], 0
    for t, b in chunks:
        total += len(b) // FRAME
        ends.append((total, t))
    cap_m, rel_m = mono(cap), mono(released)
    if np.abs(cap_m).max() < 10:
        sys.exit("capture is silent - is music playing to this Kodi?")
    results = []
    win = RATE * 2
    for start in range(RATE * 3, len(cap_m) - win - RATE, RATE * 3):
        seg = cap_m[start:start + win]
        if np.abs(seg).max() < 50:
            continue
        n = len(rel_m) + win
        nfft = 1 << (n - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(rel_m, nfft) * np.conj(np.fft.rfft(seg, nfft)), nfft)
        lag = int(np.argmax(corr[: len(rel_m) - win]))
        peak = corr[lag] / (np.linalg.norm(seg) * np.linalg.norm(rel_m[lag:lag + win]) + 1e-9)
        if peak < 0.8:
            continue
        rel_byte = lag * FRAME
        # intended local play time of that released byte
        intended = None
        for off, ln, t in index:
            if off <= rel_byte < off + ln and t:
                intended = t + int((rel_byte - off) / FRAME / RATE * 1e6)
                break
        # capture time of sample `start`
        actual = None
        prev_total, prev_t = 0, chunks[0][0]
        for tot, t in ends:
            if tot > start:
                frac = (start - prev_total) / max(1, tot - prev_total)
                actual = int(prev_t + frac * (t - prev_t))
                break
            prev_total, prev_t = tot, t
        if intended and actual:
            results.append((actual - intended) / 1000.0)
    if not results:
        sys.exit("no confident correlation windows found")
    r = np.array(results)
    print("windows=%d  mean error=%+.1f ms  median=%+.1f ms  stdev=%.1f ms  min=%+.1f max=%+.1f"
          % (len(r), r.mean(), np.median(r), r.std(), r.min(), r.max()))
    print("(positive = Kodi output is LATE vs the Sendspin timeline)")


if __name__ == "__main__":
    main()
