"""Encrypted Sendspin: crypto vectors, pairing token, framing, and a full scripted
session (guest handshake -> pairing -> re-handshake -> paired playback) against an
in-memory fake server. No network.

    python3 -m unittest tests.test_secure -v
"""

import hashlib
import json
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "service.sendspin", "resources", "lib"))

from sendspin import crypto, identity, noise, websocket  # noqa: E402
from sendspin.audio import AudioFormat  # noqa: E402
from sendspin.client import Listener, SendspinClient  # noqa: E402
from sendspin.clock import ClockSync  # noqa: E402


class CryptoTest(unittest.TestCase):
    def test_x25519_rfc7748(self):
        k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual(crypto.x25519(k, u).hex(),
                         "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        a, a_pub = crypto.x25519_keypair()
        b, b_pub = crypto.x25519_keypair()
        self.assertEqual(crypto.x25519(a, b_pub), crypto.x25519(b, a_pub))

    def test_low_order_key_rejected(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.x25519(os.urandom(32), bytes(32))

    def test_pure_matches_selected_backend(self):
        key, nonce = os.urandom(32), os.urandom(12)
        pure, best = crypto.PureChaChaPoly(key), crypto.ChaChaPoly(key)
        for size in (0, 1, 63, 64, 65, 511, 4096, 65519):
            data = os.urandom(size)
            sealed = pure.encrypt(nonce, data, b"ad")
            self.assertEqual(sealed, best.encrypt(nonce, data, b"ad"), size)
            self.assertEqual(best.decrypt(nonce, sealed, b"ad"), data)

    def test_forgery_rejected(self):
        aead = crypto.PureChaChaPoly(os.urandom(32))
        nonce = bytes(12)
        sealed = bytearray(aead.encrypt(nonce, b"hello", b""))
        sealed[0] ^= 1
        with self.assertRaises(crypto.CryptoError):
            aead.decrypt(nonce, bytes(sealed), b"")

    def test_base64url_is_strict(self):
        self.assertEqual(crypto.b64u_decode(crypto.b64u_encode(b"\xff" * 32), 32), b"\xff" * 32)
        for bad in ("abc=", "a+b/", "AB"):  # padding, wrong alphabet, non-canonical
            with self.assertRaises(ValueError):
                crypto.b64u_decode(bad)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_token_reference_vector(self):
        self.assertEqual(
            identity.pairing_token(bytes(range(32)), bytes(range(0xE0, 0x100))),
            "SP:0AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP6BYPC4PSOLZXH5DU6V97M5XXO74HR6LZ7J5PW674PT6X37T6757Y")
        self.assertEqual(identity.SENTINEL_ID, "GFsV9tLaSQm9HcFWpKsgYQOr7wFTvNUtkmFwuVz3zoo")

    def test_identity_persists(self):
        a = identity.Identity(self.dir)
        b = identity.Identity(self.dir)
        self.assertEqual((a.client_id, a.pairing_psk), (b.client_id, b.pairing_psk))
        self.assertEqual(len(a.client_id), 43)

    def test_psk_resolution(self):
        ident = identity.Identity(self.dir)
        psk = os.urandom(32)
        ident.add_pairing("server-a", psk)

        def ask(p, cat, server="server-a", fallback=True):
            payload = json.dumps({"psk_id": identity.psk_id(p), "psk_category": cat}).encode()
            return ident.resolve_psk(payload, server, fallback)

        self.assertEqual(ask(psk, "lt"), (psk, "lt"))
        self.assertEqual(ask(ident.pairing_psk, "pr"), (ident.pairing_psk, "pr"))
        self.assertEqual(ask(identity.SENTINEL_PSK, "sn"), (identity.SENTINEL_PSK, "sn"))
        self.assertEqual(ask(os.urandom(32), "lt"), (identity.SENTINEL_PSK, "sn"))  # unknown: unpaired
        with self.assertRaises(ValueError):
            ask(psk, "lt", server="server-b")  # a record only works for its own server
        with self.assertRaises(ValueError):
            ask(os.urandom(32), "lt", fallback=False)  # no fallback in a re-handshake

    def test_pairings_are_capped(self):
        ident = identity.Identity(self.dir)
        for i in range(identity.MAX_PAIRINGS + 3):
            ident.add_pairing("server-%d" % i, os.urandom(32))
        names = [r["server_id"] for r in ident.pairings()]
        self.assertEqual(len(names), identity.MAX_PAIRINGS)
        self.assertIn("server-%d" % (identity.MAX_PAIRINGS + 2), names)


class FramingTest(unittest.TestCase):
    def test_small_message_is_one_frame(self):
        self.assertEqual(noise.frames(0, b"{}"), [b"\x00{}"])

    def test_fragment_round_trip(self):
        body = os.urandom(3 * noise.MAX_PLAINTEXT)
        parts = noise.frames(8, body)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= noise.MAX_PLAINTEXT for p in parts))
        r = noise.Reassembler()
        results = [r.feed(p) for p in parts]
        self.assertEqual(results[:-1], [None] * (len(parts) - 1))
        self.assertEqual(results[-1], (8, body))

    def test_bad_fragments(self):
        for seq in ([b"\x01\x00abc"],  # continuation with nothing started
                    [b"\x01\x02\x05ab", b"\x00{}"],  # message inside a fragmented one
                    [b"\x01\x06\x05ab"],  # reserved flag bit
                    [b"\x02abc"]):  # reserved message type
            r = noise.Reassembler()
            with self.assertRaises(noise.NoiseError):
                for p in seq:
                    r.feed(p)


# -- a scripted Sendspin server (Noise initiator) ------------------------------------

class Initiator:
    """Server side of KKpsk2, for the tests only."""

    def __init__(self, s_priv, s_pub, client_pub, prologue, psk):
        self.s_priv, self.rs, self.psk = s_priv, client_pub, psk
        self.h = hashlib.sha256(noise.PROTOCOL_NAME).digest()
        self.ck = self.h
        self.cipher = noise.CipherState()
        for d in (prologue, s_pub, client_pub):
            self.mix_hash(d)

    def mix_hash(self, d):
        self.h = hashlib.sha256(self.h + d).digest()

    def mix_key(self, ikm):
        self.ck, k = noise.hkdf(self.ck, ikm, 2)
        self.cipher = noise.CipherState(k)

    def write1(self, payload):
        self.e_priv, e_pub = crypto.x25519_keypair()
        self.mix_hash(e_pub)
        self.mix_key(e_pub)
        self.mix_key(crypto.x25519(self.e_priv, self.rs))  # es
        self.mix_key(crypto.x25519(self.s_priv, self.rs))  # ss
        c = self.cipher.encrypt(self.h, payload)
        self.mix_hash(c)
        return e_pub + c

    def read2(self, msg):
        re = msg[:32]
        self.mix_hash(re)
        self.mix_key(re)
        self.mix_key(crypto.x25519(self.e_priv, re))  # ee
        self.mix_key(crypto.x25519(self.s_priv, re))  # se
        self.ck, th, tk = noise.hkdf(self.ck, self.psk, 3)
        self.mix_hash(th)
        self.cipher = noise.CipherState(tk)
        payload = self.cipher.decrypt(self.h, msg[32:])
        self.mix_hash(msg[32:])
        k1, k2 = noise.hkdf(self.ck, b"", 2)
        return payload, noise.Transport(send=noise.CipherState(k1), recv=noise.CipherState(k2),
                                        handshake_hash=self.h)


def _msg(typ, **payload):
    return json.dumps({"type": typ, "payload": payload}, separators=(",", ":"))


class FakeServer:
    """Plays Music Assistant: guest playback, then pairs with the pairing PSK and plays paired."""

    PLAYER = ["player@v1", "metadata@v1", "controller@v1"]

    def __init__(self, client_pub, pairing_psk, known_psk=None):
        self.s_priv, self.s_pub = crypto.x25519_keypair()
        self.server_id = crypto.b64u_encode(self.s_pub)
        self.client_pub, self.pairing_psk = client_pub, pairing_psk
        self.known_psk = known_psk  # a long-term PSK from an earlier pairing
        self.out = queue.Queue()
        self.transport = None
        self.pending = None  # re-handshake initiator
        self.long_term = None
        self.log = []
        self.done = threading.Event()

    # client -> server
    def on_text(self, text):
        msg = json.loads(text)
        if msg["type"] == "client/init":
            init = _msg("server/init", server_id=self.server_id, version=1)
            psk, cat = (self.known_psk, "lt") if self.known_psk else (identity.SENTINEL_PSK, "sn")
            self.hs = Initiator(self.s_priv, self.s_pub, self.client_pub, text.encode() + init.encode(), psk)
            m1 = self.hs.write1(json.dumps({"psk_id": identity.psk_id(psk), "psk_category": cat}).encode())
            self.out.put((websocket.OP_TEXT, init))
            self.out.put((websocket.OP_TEXT, _msg("noise/handshake", data=crypto.b64u_encode(m1))))
        elif msg["type"] == "noise/handshake":
            payload, self.transport = self.hs.read2(crypto.b64u_decode(msg["payload"]["data"]))
            assert payload == b"{}"
            self.send(_msg("server/hello", name="Fake MA", languages=["en"]))

    def on_binary(self, data):
        plain = self.transport.open(data)
        assert plain[0] == 0, plain[:1]
        msg = json.loads(plain[1:])
        typ, p = msg["type"], msg["payload"]
        self.log.append(typ)
        if self.pending is not None and typ != "noise/handshake":
            return  # re-handshake quiet period: nothing new until the next server/activate
        if typ == "client/time":
            t = p["client_transmitted"]
            self.send(_msg("server/time", client_transmitted=t, server_received=t + 5, server_transmitted=t + 6))
        elif typ == "client/hello":
            self.hello = p
            self.send(_msg("server/activate", activities=["playback"], active_roles=self.PLAYER))
            self.send(_msg("group/update", playback_state="stopped", group_id=crypto.b64u_encode(bytes(32)),
                           group_name="Room"))
        elif typ == "client/state" and self.long_term is None and not self.known_psk and self.pending is None:
            self.rehandshake(self.pairing_psk, "pr")  # operator pasted the token
        elif typ == "noise/handshake":
            _, new = self.pending.read2(crypto.b64u_decode(p["data"]))
            self.transport, self.pending = new, None
            if self.long_term is None:
                self.send(_msg("server/activate", activities=["pairing"], active_roles=[],
                               pairing={"method": "pairing_psk"}))
            else:
                self.play()
        elif typ == "client/pair-finalize":
            self.long_term = crypto.b64u_decode(p["long_term_psk"], 32)
            self.send(_msg("server/pair-finalize"))
            self.rehandshake(self.long_term, "lt")
        elif typ == "client/state" and (self.known_psk or self.long_term) and self.pending is None:
            self.play()

    def rehandshake(self, psk, cat):
        self.pending = Initiator(self.s_priv, self.s_pub, self.client_pub, self.transport.handshake_hash, psk)
        m1 = self.pending.write1(json.dumps({"psk_id": identity.psk_id(psk), "psk_category": cat}).encode())
        self.send(_msg("noise/handshake", data=crypto.b64u_encode(m1)))

    def play(self):
        if self.done.is_set():
            return
        self.send(_msg("server/activate", activities=["playback"], active_roles=self.PLAYER))
        self.send(_msg("stream/start", server_transmitted=0,
                       player={"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16}))
        chunk = struct.pack(">BqI", 4, 123456, 0xFFFFFFFF) + bytes(400)
        self.out.put((websocket.OP_BINARY, self.transport.seal(chunk)))
        self.done.set()

    def send(self, text):
        self.out.put((websocket.OP_BINARY, self.transport.seal(b"\x00" + text.encode())))


class FakeWs:
    server = None

    def __init__(self, url):
        self.closed = False

    def connect(self):
        pass

    def send_text(self, text):
        if self.closed:
            raise websocket.WebSocketClosed("closed")
        self.server.on_text(text)

    def send_binary(self, data):
        if self.closed:
            raise websocket.WebSocketClosed("closed")
        self.server.on_binary(data)

    def recv(self):
        while not self.closed:
            try:
                return self.server.out.get(timeout=0.1)
            except queue.Empty:
                continue
        raise websocket.WebSocketClosed("closed")

    def close(self, code=1000):
        self.closed = True

    abort = close


class Pipeline:
    volume, muted, static_delay_us, lead_us, fmt = 100, False, 0, 3_000_000, None

    def __init__(self):
        self._clock = ClockSync()
        self.pushed = []

    def start(self, fmt, continuous=False):
        self.fmt = fmt
        return 1, False

    def clear(self, from_timestamp=None):
        return 1

    def end(self):
        pass

    def push(self, ts, pcm):
        self.pushed.append((ts, len(pcm)))


class Events(Listener):
    def __init__(self):
        self.paired = []
        self.formats = []

    def on_paired(self, name):
        self.paired.append(name)

    def on_stream_start(self, fmt, generation, continuous=False):
        self.formats.append(fmt)


class SecureSessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        import sendspin.client as client_mod
        real = client_mod.WebSocket
        client_mod.WebSocket = FakeWs
        self.addCleanup(setattr, client_mod, "WebSocket", real)

    def _run(self, ident, server):
        FakeWs.server = server
        pipeline, events = Pipeline(), Events()
        client = SendspinClient("ws://fake", "kodi-legacy", "Kodi Test", pipeline, events, identity=ident,
                                mode="secure")
        client.start()
        self.assertTrue(server.done.wait(10), "server log: %s" % server.log)
        for _ in range(50):
            if pipeline.pushed:
                break
            threading.Event().wait(0.05)
        client.stop()
        return pipeline, events

    def test_guest_then_pairing_then_paired_playback(self):
        ident = identity.Identity(self.dir)
        server = FakeServer(ident.public, ident.pairing_psk)
        pipeline, events = self._run(ident, server)
        hello = server.hello
        self.assertNotIn("client_id", hello)
        self.assertEqual(hello["supported_pair_methods"], {"pairing_psk": {"locations": ["operator"]}})
        self.assertEqual(hello["unpaired_access"], {"enabled": True})
        self.assertEqual(events.paired, ["Fake MA"])
        self.assertEqual(events.formats, [AudioFormat(48000, 2, 16)])
        self.assertEqual(pipeline.pushed, [(123456, 400)])  # 13-byte header stripped
        records = ident.pairings()
        self.assertEqual([r["server_id"] for r in records], [server.server_id])
        self.assertEqual(crypto.b64u_decode(records[0]["psk"]), server.long_term)

    def test_reconnect_uses_the_long_term_key(self):
        ident = identity.Identity(self.dir)
        first = FakeServer(ident.public, ident.pairing_psk)
        self._run(ident, first)
        again = FakeServer(ident.public, ident.pairing_psk, known_psk=first.long_term)
        again.s_priv, again.s_pub, again.server_id = first.s_priv, first.s_pub, first.server_id
        pipeline, _ = self._run(ident, again)
        self.assertEqual(pipeline.pushed, [(123456, 400)])
        self.assertNotIn("client/pair-init", again.log)


class RefusingServer:
    """Answers client/init the way a server without the encrypted wire does."""

    def __init__(self):
        self.out = queue.Queue()

    def on_text(self, text):
        self.out.put((websocket.OP_TEXT, _msg("server/error", reason="malformed")))


class AutoModeTest(SecureSessionTest):
    def test_refused_secure_falls_back_to_legacy(self):
        from sendspin.client import ServerRefused
        FakeWs.server = RefusingServer()
        refused = []
        events = Events()
        events.on_refused = lambda reason, legacy: refused.append((reason, legacy))
        c = SendspinClient("ws://fake", "kodi-legacy", "Kodi", Pipeline(), events,
                           identity=identity.Identity(self.dir))
        self.assertEqual(c.mode, "auto")
        with self.assertRaises(ServerRefused) as cm:
            c._session()
        c._on_refused(cm.exception)
        self.assertTrue(c._try_legacy)
        self.assertEqual(refused, [("malformed", False)])

    test_guest_then_pairing_then_paired_playback = None
    test_reconnect_uses_the_long_term_key = None


if __name__ == "__main__":
    unittest.main()
