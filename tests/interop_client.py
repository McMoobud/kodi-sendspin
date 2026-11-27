"""Run our SendspinClient headless against a server and count the audio it receives.

No Kodi and no speakers: audio chunks are counted in the pipeline, never played.

    python3 tests/interop_client.py --identity DIR [--mode secure|legacy|auto] [--seconds 10]
    python3 tests/interop_client.py --identity DIR --print-token   # show the pairing token

``DIR`` holds identity.json and pairings.json; reuse it to reconnect as the same player.
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service.sendspin", "resources", "lib"))

from sendspin.audio import AudioPipeline  # noqa: E402
from sendspin.client import Listener, SendspinClient  # noqa: E402
from sendspin.clock import ClockSync  # noqa: E402
from sendspin.identity import Identity  # noqa: E402


class Recorder(Listener):
    def __init__(self):
        self.events = []

    def _add(self, *event):
        self.events.append(event)
        print("CLIENT_EVENT", *event, flush=True)

    def on_connected(self, server_name):
        self._add("connected", server_name)

    def on_disconnected(self):
        self._add("disconnected")

    def on_refused(self, reason, legacy):
        self._add("refused", reason, "legacy=%s" % legacy)

    def on_paired(self, server_name):
        self._add("paired", server_name)

    def on_stream_start(self, fmt, generation, continuous=False):
        self._add("stream_start", repr(fmt))

    def on_stream_end(self):
        self._add("stream_end")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://127.0.0.1:8927/sendspin")
    parser.add_argument("--identity", required=True, help="folder for identity.json / pairings.json")
    parser.add_argument("--mode", default="secure", choices=["secure", "legacy", "auto"])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--print-token", action="store_true", help="print the pairing token and exit")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="CLIENT_LOG %(name)s %(levelname)s %(message)s")

    ident = Identity(args.identity)
    if args.print_token:
        print(ident.token)
        return
    pipeline = AudioPipeline(ClockSync())
    received_bytes = 0
    push = pipeline.push

    def counting_push(server_ts, pcm):
        nonlocal received_bytes
        if pipeline.active:
            received_bytes += len(pcm)
        push(server_ts, pcm)

    pipeline.push = counting_push
    recorder = Recorder()
    client = SendspinClient(args.url, "kodi-test", "Kodi Interop", pipeline, recorder, identity=ident, mode=args.mode)
    client.start()
    time.sleep(args.seconds)
    category = client._psk_category  # noqa: SLF001 - peek at the session for the summary
    client.stop()

    fmt = pipeline.fmt
    secs = received_bytes / (fmt.sample_rate * fmt.frame_bytes) if fmt else 0.0
    print("SUMMARY mode=%s client_id=%s psk=%s audio_bytes=%d (%.1fs of %s) chunks=%d pairings=%d events=%s"
          % (args.mode, ident.client_id if args.mode != "legacy" else "kodi-test", category, received_bytes, secs,
             fmt, pipeline.stats["received"], len(ident.pairings()), [e[0] for e in recorder.events]), flush=True)


if __name__ == "__main__":
    main()
