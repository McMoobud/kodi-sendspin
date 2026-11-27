"""Local Sendspin test server built on the real aiosendspin library (dev machine only).

It behaves like Music Assistant: guests (unpaired clients with unpaired access on) are
approved automatically, a pasted pairing token pairs that client, and every connected
player gets a 440 Hz sine (48 kHz, 16-bit, stereo PCM) pushed in real time.

    .venv/bin/python tests/interop_server.py --seconds 30
    .venv/bin/python tests/interop_server.py --seconds 30 --legacy
    .venv/bin/python tests/interop_server.py --seconds 60 --pair-token SP:0...

Clients connect to ws://127.0.0.1:8927/sendspin. Events go to stdout, one per line:
CONNECTED, PAIRED, STREAMING, STREAM_END, DISCONNECTED, ERROR, then the pairing store.
"""

import argparse
import asyncio
import logging
import math
import sys
import time

from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.noise.pairing_token import decode_psk_token
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import AudioFormat, SendspinServer
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent

FORMAT = AudioFormat(sample_rate=48000, bit_depth=16, channels=2)
CHUNK_FRAMES = 960  # 20 ms
MAX_AHEAD_US = 1_500_000  # how far ahead of real time audio is pushed


def say(*parts):
    print(" ".join(str(p) for p in parts), flush=True)


def sine_chunks(freq=440.0, volume=0.3):
    """Endless 20 ms chunks of a stereo int16 sine."""
    phase, step = 0, 2 * math.pi * freq / FORMAT.sample_rate
    while True:
        out = bytearray()
        for i in range(CHUNK_FRAMES):
            s = int(volume * 32767 * math.sin(step * (phase + i)))
            out += s.to_bytes(2, "little", signed=True) * 2
        phase += CHUNK_FRAMES
        yield bytes(out)


class InteropServer:
    def __init__(self, args):
        self.args = args
        self.token = decode_psk_token(args.pair_token) if args.pair_token else None
        self.store = InMemoryServerPairingStore()
        self.server = None
        self.tasks = set()
        self.streaming = set()  # group ids with a running sine task

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def on_event(self, _server, event):
        if isinstance(event, ClientConnectedEvent):
            self.spawn(self.on_connected(event.client_id))
        elif isinstance(event, ClientDisconnectedEvent):
            say("DISCONNECTED", event.client_id, "reason=%s" % event.goodbye_reason)

    async def on_connected(self, client_id):
        client = self.server.get_client(client_id)
        security = client.connection_security if client else None
        say("CONNECTED", client_id, "encrypted=%s" % (security is not None),
            "psk=%s" % (security.psk_category.value if security else "none"))
        if client is None or security is None:
            return  # legacy: nothing to approve or pair
        try:
            # Same rule as Music Assistant's _auto_trust_guest_access.
            if client.info.unpaired_access.enabled and not client.is_paired \
                    and await self.store.trusted_unpaired(client_id) is None:
                await self.server.trust_unpaired(client_id)
                say("GUEST_APPROVED", client_id)
            if self.token and self.token.client_id == client_id and not client.is_paired:
                # Same call as Music Assistant's "Pairing token" flow (pair_with_token).
                attempt = PairingAttempt(PairMethod.PAIRING_PSK, client_id=client_id,
                                         pairing_psk=self.token.pairing_psk)
                async with asyncio.timeout(60):
                    await self.server.initiate_pairing(client_id, attempt)
                say("PAIRED", client_id, "psk=%s" % client.connection_security.psk_category.value)
        except Exception as exc:  # noqa: BLE001 - report and carry on
            say("ERROR", client_id, "%s: %s" % (type(exc).__name__, exc))

    async def watch_players(self):
        """Start the sine on the group of every connected player with an active player role."""
        while True:
            for client in self.server.connected_clients:
                group = client.group
                if client.role("player@v1") and group.group_id not in self.streaming:
                    self.streaming.add(group.group_id)
                    self.spawn(self.stream(group))
            await asyncio.sleep(0.2)

    async def stream(self, group):
        say("STREAMING", "group=%s" % group.group_id, "clients=%s" % [c.client_id for c in group.clients],
            "pcm 48000Hz/16bit/2ch sine 440Hz")
        stream = group.start_stream()
        pushed, t0 = 0, time.monotonic()
        try:
            for pcm in sine_chunks():
                if not any(c.connection is not None for c in group.clients):
                    break
                stream.prepare_audio(pcm, FORMAT)
                await stream.commit_audio()
                pushed += CHUNK_FRAMES
                await stream.sleep_to_limit_buffer(MAX_AHEAD_US)
        except Exception as exc:  # noqa: BLE001
            say("ERROR", "stream:", "%s: %s" % (type(exc).__name__, exc))
        finally:
            if not stream.is_stopped:
                stream.stop()
            self.streaming.discard(group.group_id)
            say("STREAM_END", "group=%s" % group.group_id, "audio=%.1fs" % (pushed / FORMAT.sample_rate),
                "wall=%.1fs" % (time.monotonic() - t0))

    async def print_store(self):
        say("STORE records:")
        for r in await self.store.list_records():
            say("  client_id=%s psk_id=%s methods=%s" % (r.client_id, r.psk_id, [m.value for m in r.pair_methods]))
        say("STORE trusted_unpaired:")
        for t in await self.store.list_trusted_unpaired():
            say("  client_id=%s roles=%s" % (t.client_id, sorted(t.approved_roles)))
        say("STORE staged_pairing_psks:")
        for s in await self.store.list_staged_pairing_psks():
            say("  psk_id=%s" % s.psk_id)

    async def run(self):
        args = self.args
        self.server = SendspinServer(
            asyncio.get_running_loop(), Identity.generate(), "Interop Test Server",
            pairing_store=self.store, allow_unencrypted=args.legacy, allow_noncompliant_clients=True)
        self.server.add_event_listener(self.on_event)
        await self.server.start_server(port=args.port, host="127.0.0.1", discover_clients=False)
        say("LISTENING ws://127.0.0.1:%d/sendspin" % args.port, "server_id=%s" % self.server.id,
            "legacy=%s" % args.legacy, "pair_token=%s" % ("yes" if self.token else "no"))
        self.spawn(self.watch_players())
        try:
            await asyncio.sleep(args.seconds)
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.server.close()
            await self.print_store()
            say("EXIT")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8927)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--legacy", action="store_true", help="accept unencrypted clients (allow_unencrypted)")
    parser.add_argument("--pair-token", help="pair this client (SP:0... token) once it connects")
    parser.add_argument("--debug", action="store_true", help="aiosendspin debug logging")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if args.debug else logging.WARNING,
                        format="LOG %(levelname)s %(name)s: %(message)s")
    asyncio.run(InteropServer(args).run())


if __name__ == "__main__":
    main()
