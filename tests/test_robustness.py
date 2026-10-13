"""Robustness tests: server URL parsing, hostile mDNS replies, WebSocket limits,
session cleanup and Kodi button de-duplication (no network, fake Kodi modules).

    python3 -m unittest discover -s tests -v
"""

import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "fakekodi"))
sys.path.insert(0, os.path.join(HERE, "..", "service.sendspin", "resources", "lib"))

import kodi_bridge  # noqa: E402
from sendspin import discovery, websocket  # noqa: E402
from sendspin.client import SendspinClient  # noqa: E402


class ServerUrlTest(unittest.TestCase):
    def test_forms(self):
        n = kodi_bridge.normalise_server_url
        self.assertEqual(n("192.168.1.10"), "ws://192.168.1.10:8927/sendspin")
        self.assertEqual(n("192.168.1.10:9000"), "ws://192.168.1.10:9000/sendspin")
        self.assertEqual(n("192.168.1.10/sendspin"), "ws://192.168.1.10:8927/sendspin")
        self.assertEqual(n("192.168.1.10/other"), "ws://192.168.1.10:8927/other")
        self.assertEqual(n("ws://ma.local:8927/sendspin"), "ws://ma.local:8927/sendspin")
        self.assertEqual(n(" ma.local "), "ws://ma.local:8927/sendspin")
        self.assertEqual(n("[fe80::1]:8927"), "ws://[fe80::1]:8927/sendspin")
        self.assertIsNone(n("host:notaport"))
        self.assertIsNone(n("ws://"))

    def test_typos_are_invalid_not_fatal(self):
        n = kodi_bridge.normalise_server_url
        self.assertIsNone(n("ws://[::1"))
        self.assertIsNone(n("[fe80::1"))


def _name(name):
    return discovery._encode_name(name)


def _reply(*records, an=None):
    """Build an mDNS response from (name, type, rdata) tuples."""
    body = b"".join(_name(n) + struct.pack(">HHIH", t, 1, 120, len(rd)) + rd for n, t, rd in records)
    count = len(records) if an is None else an
    return struct.pack(">HHHHHH", 0, 0x8400, 0, count, 0, 0) + body


INST = "Music Assistant." + discovery.SERVICE
TARGET = "ma.local"


def _good_records(path=b"path=/sendspin"):
    return [
        (discovery.SERVICE, discovery.T_PTR, _name(INST)),
        (INST, discovery.T_SRV, struct.pack(">HHH", 0, 0, 8927) + _name(TARGET)),
        (INST, discovery.T_TXT, bytes([len(path)]) + path),
        (TARGET, discovery.T_A, socket.inet_aton("192.168.1.10")),
    ]


def _empty():
    return {"instances": set(), "srv": {}, "txt": {}, "addrs": {}}


class DiscoveryTest(unittest.TestCase):
    def test_good_reply(self):
        found = _empty()
        discovery._collect(_reply(*_good_records()), found)
        self.assertTrue(discovery._complete(INST.lower(), found))
        servers = discovery._servers(found)
        self.assertEqual(servers[0]["url"], "ws://192.168.1.10:8927/sendspin")

    def test_empty_srv_record_is_ignored(self):
        found = _empty()
        bad = (INST, discovery.T_SRV, b"")
        discovery._collect(_reply(bad), found)  # must not raise
        self.assertEqual(found["srv"], {})

    def test_truncated_and_garbage_packets(self):
        found = _empty()
        good = _reply(*_good_records())
        for data in (b"", b"\x00" * 5, good[:40], good[:-3], os.urandom(200), _reply(an=50)):
            discovery._collect(data, found)  # must not raise

    def test_waits_for_address_and_txt(self):
        recs = _good_records(path=b"path=/custom")
        found = _empty()
        discovery._collect(_reply(recs[0], recs[1]), found)  # PTR + SRV only
        self.assertFalse(discovery._complete(INST.lower(), found))
        discovery._collect(_reply(recs[2], recs[3]), found)  # TXT + A in a later packet
        self.assertTrue(discovery._complete(INST.lower(), found))
        self.assertEqual(discovery._servers(found)[0]["url"], "ws://192.168.1.10:8927/custom")


class _FakeSock:
    def __init__(self, data):
        self.data = data

    def recv(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def shutdown(self, how):
        pass

    def close(self):
        pass

    def sendall(self, data):
        pass


def _ws_with(data):
    ws = websocket.WebSocket("ws://127.0.0.1:1/x")
    ws._sock, ws.closed = _FakeSock(data), False
    return ws


class WebSocketLimitTest(unittest.TestCase):
    def test_huge_frame_header_rejected_before_reading(self):
        frame = bytes([0x82, 127]) + struct.pack(">Q", 1 << 40)
        with self.assertRaises(websocket.WebSocketError):
            _ws_with(frame).recv()

    def test_endless_fragments_rejected(self):
        part = b"x" * 65535
        first = bytes([0x02, 126]) + struct.pack(">H", len(part)) + part  # BINARY, not FIN
        cont = bytes([0x00, 126]) + struct.pack(">H", len(part)) + part   # CONT, not FIN
        count = websocket.MAX_MESSAGE_BYTES // len(part) + 2
        with self.assertRaises(websocket.WebSocketError):
            _ws_with(first + cont * count).recv()

    def test_empty_continuation_flood_rejected(self):
        first = bytes([0x02, 1]) + b"x"   # BINARY, not FIN
        empty = bytes([0x00, 0])          # CONT, not FIN, no payload
        last = bytes([0x80, 1]) + b"y"    # CONT, FIN
        with self.assertRaises(websocket.WebSocketError):
            _ws_with(first + empty * (websocket.MAX_FRAGMENTS + 5) + last).recv()

    def test_continuation_without_message_rejected(self):
        orphan = bytes([0x80, 1]) + b"x"  # CONT, FIN, but no message started
        with self.assertRaises(websocket.WebSocketError):
            _ws_with(orphan + bytes([0x81, 5]) + b"hello").recv()

    def test_normal_message(self):
        payload = b"hello"
        ws = _ws_with(bytes([0x81, len(payload)]) + payload)
        self.assertEqual(ws.recv(), (websocket.OP_TEXT, "hello"))


class _FakeWs:
    """One session: hello is sent, then the server sends a broken message."""

    instances = []

    def __init__(self, url):
        self.closed = False
        self.sent = []
        _FakeWs.instances.append(self)

    def connect(self):
        pass

    def send_text(self, text):
        if self.closed:
            raise websocket.WebSocketClosed("closed")
        self.sent.append(json.loads(text)["type"])

    def recv(self):
        return websocket.OP_TEXT, "{not json"

    def close(self):
        self.closed = True


class _Pipeline:
    volume, muted, static_delay_us, lead_us = 100, False, 0, 3_000_000

    def end(self):
        pass


class SessionCleanupTest(unittest.TestCase):
    def test_failed_session_closes_its_socket(self):
        import sendspin.client as client_mod
        real = client_mod.WebSocket
        client_mod.WebSocket = _FakeWs
        try:
            c = SendspinClient("ws://x", "id", "name", _Pipeline())
            with self.assertRaises(ValueError):
                c._session()
            ws = _FakeWs.instances[-1]
            self.assertTrue(ws.closed)
            time.sleep(0.2)
            # the time thread of a dead session never writes again
            before = list(ws.sent)
            time.sleep(0.2)
            self.assertEqual(ws.sent, before)
        finally:
            client_mod.WebSocket = real


class _StuckSock:
    """A server that stopped reading: writes block until the socket is shut down."""

    def __init__(self):
        self.down = threading.Event()

    def sendall(self, data):
        self.down.wait()
        raise OSError("socket shut down")

    def recv(self, n):
        self.down.wait()
        return b""

    def shutdown(self, how):
        self.down.set()

    def close(self):
        self.down.set()


class StopTest(unittest.TestCase):
    def test_stop_does_not_wait_on_a_stalled_server(self):
        c = SendspinClient("ws://x", "id", "name", _Pipeline())
        ws = websocket.WebSocket("ws://x")
        ws._sock, ws.closed = _StuckSock(), False
        c._ws = ws
        done = threading.Event()
        threading.Thread(target=lambda: (c.stop(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(3), "stop() hung on a blocked write")
        self.assertTrue(ws.closed)


class _RecPipeline(_Pipeline):
    fmt = None

    def __init__(self):
        self.calls = []

    def start(self, fmt, continuous=False):
        self.calls.append(("start", fmt))
        self.fmt = fmt
        return 1, False

    def end(self):
        self.calls.append(("end",))

    def push(self, ts, pcm):
        self.calls.append(("push", len(pcm)))


class StreamFormatTest(unittest.TestCase):
    def _start(self, pipeline, **player):
        c = SendspinClient("ws://x", "id", "name", pipeline)
        p = {"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16}
        p.update(player)
        c._on_text({"type": "stream/start", "payload": {"player": p}}, 0)
        return c

    def test_offered_format_accepted(self):
        pl = _RecPipeline()
        self._start(pl)
        self.assertEqual(pl.calls[0][0], "start")

    def test_format_not_offered_is_rejected(self):
        for bad in ({"sample_rate": 0}, {"channels": 0}, {"bit_depth": 12}, {"codec": "flac"}):
            pl = _RecPipeline()
            self._start(pl, **bad)
            self.assertEqual(pl.calls, [("end",)], bad)

    def test_partial_frames_dropped(self):
        pl = _RecPipeline()
        c = self._start(pl)
        head = bytes([4]) + struct.pack(">q", 0)
        c._on_binary(head + bytes(40))  # 10 whole 16-bit stereo frames
        c._on_binary(head + bytes(41))
        self.assertEqual([x for x in pl.calls if x[0] == "push"], [("push", 40)])


class SkipDedupTest(unittest.TestCase):
    def _bridge(self):
        b = kodi_bridge.Bridge.__new__(kodi_bridge.Bridge)
        b._lock = threading.Lock()
        b._last_skip_path, b._last_skip_at = None, 0.0
        b.sent = []
        b.client = type("C", (), {"send_controller": lambda _self, a: b.sent.append(a)})()
        return b

    def test_probe_and_open_count_once(self):
        b = self._bridge()
        b.on_remote_skip("next", "/control/next/3")
        b.on_remote_skip("next", "/control/next/3")
        self.assertEqual(b.sent, ["next"])

    def test_later_press_on_same_url_counts(self):
        b = self._bridge()
        b.on_remote_skip("next", "/control/next/3")
        b._last_skip_at -= b.SKIP_DEDUP_S + 0.1  # a later press, same generation URL
        b.on_remote_skip("next", "/control/next/3")
        self.assertEqual(b.sent, ["next", "next"])


if __name__ == "__main__":
    unittest.main()
