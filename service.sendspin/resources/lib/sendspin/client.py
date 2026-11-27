"""Sendspin player client.

Two wires:

- **secure** (Sendspin 1.0): ``client/init`` / ``server/init``, a Noise KKpsk2 handshake,
  then every message encrypted in a WebSocket binary frame. Works unpaired ("guest
  access" in Music Assistant) or paired through the pairing token.
- **legacy**: the old unencrypted "transition mode". Music Assistant accepts it only
  while its Sendspin provider option "Allow legacy clients" is on.

``mode="auto"`` tries secure first and switches to legacy when the server does not
speak it (and back again if legacy is refused).

Threading model: one reader thread (``_run``) owns the socket receive side and
dispatches messages; one timer thread sends ``client/time`` bursts. Callbacks
are invoked on the reader thread and must be quick.
"""

import json
import logging
import os
import random
import struct
import threading

from .audio import AudioFormat
from .clock import ClockSync, measure, now_us
from .crypto import b64u_decode, b64u_encode
from .identity import LONG_TERM, PAIRING
from .noise import SUITE, TYPE_JSON, NoiseError, Reassembler, Responder, frames
from .websocket import OP_BINARY, OP_TEXT, WebSocket, WebSocketError

log = logging.getLogger("sendspin.client")

PROTOCOL_VERSION = 1
BIN_AUDIO_CHUNK = 4
LEGACY_COMMANDS = ["volume", "mute"]  # set_static_delay is rejected in the legacy hello by MA 2.10
SECURE_COMMANDS = ["volume", "mute"]
HANDSHAKE_TIMEOUT_S = 30.0
BURST = 8  # client/time messages per burst (spec minimum)
BURST_GAPS_S = [0.5, 1.0, 2.0]  # fast lock first...
BURST_GAP_S = 5.0  # ...then a burst every 5 s (spec: at most 10 s apart)

MODE_AUTO, MODE_SECURE, MODE_LEGACY = "auto", "secure", "legacy"
ACTIVITIES = ("playback", "pairing")


class ServerRefused(WebSocketError):
    """The server answered with server/error (or refused our wire)."""

    def __init__(self, reason, before_handshake=True):
        super().__init__("server refused the connection: %s" % reason)
        self.reason = reason
        self.before_handshake = before_handshake


class ProtocolError(WebSocketError):
    """The server broke the protocol: close the connection."""


class Listener:
    """Override what you need. All callbacks run on the client thread."""

    def on_connected(self, server_name):
        pass

    def on_disconnected(self):
        pass

    def on_refused(self, reason, legacy):
        """The server refused us (``legacy``: it was the unencrypted wire)."""

    def on_paired(self, server_name):
        pass

    def on_unpaired(self, server_name):
        pass

    def output_running(self):
        """True if the output (Kodi) is currently playing our stream."""
        return False

    def on_stream_start(self, fmt, generation, continuous=False):
        pass

    def on_stream_clear(self, generation):
        pass

    def on_stream_end(self):
        pass

    def on_metadata(self, metadata):
        pass

    def on_volume(self, volume, muted):
        pass

    def on_static_delay(self, delay_ms):
        pass

    def on_group(self, payload):
        pass


def _dumps(msg):
    return json.dumps(msg, separators=(",", ":"))


class SendspinClient:
    def __init__(self, url, client_id, name, pipeline, listener=None, version="0.1.0",
                 product="Kodi", buffer_capacity=2_000_000, formats=None, identity=None,
                 mode=None, unpaired_access=True):
        self.url = url
        self.client_id = client_id  # legacy wire only; the secure wire uses identity.client_id
        self.name = name
        self.pipeline = pipeline
        self.listener = listener or Listener()
        self.version = version
        self.product = product
        self.buffer_capacity = buffer_capacity
        self.formats = formats or [
            {"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16},
            {"codec": "pcm", "channels": 2, "sample_rate": 44100, "bit_depth": 16},
        ]
        self.identity = identity
        self.mode = mode or (MODE_AUTO if identity is not None else MODE_LEGACY)
        if self.mode != MODE_LEGACY and identity is None:
            raise ValueError("the secure wire needs an identity")
        self.unpaired_access = bool(unpaired_access)
        self.clock = pipeline._clock if hasattr(pipeline, "_clock") else ClockSync()
        self.available = True
        self.connected = False
        self.server_name = None
        self.metadata = {}
        self._ws = None
        self._stop = threading.Event()
        self._thread = None
        self._send_lock = threading.Lock()
        self._try_legacy = self.mode == MODE_LEGACY
        self._reset_session()

    def _reset_session(self):
        self._noise = None  # Transport while on the secure wire
        self._reasm = Reassembler()
        self._legacy_wire = True
        self._server_id = None
        self._psk_category = None
        self._quiet = False  # re-handshake: no application messages until server/activate
        self._activated = False
        self._playback = False
        self._roles = []
        self._pairing_index = 0
        self._pending_psk = None
        self._state_pending = False
        self._time_reply = None
        self._time_tx = None
        self._time_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sendspin-client", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            # Say goodbye if the server is listening, but never let a stalled one hold up shutdown.
            goodbye = threading.Thread(target=self._goodbye, args=(ws, "shutdown"), name="sendspin-goodbye",
                                       daemon=True)
            goodbye.start()
            goodbye.join(1.0)
            ws.abort()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _goodbye(self, ws, reason):
        try:
            self._send_on(ws, {"type": "client/goodbye", "payload": {"reason": reason}}, force=True)
            ws.close()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except ServerRefused as exc:
                if not self._stop.is_set():
                    self._on_refused(exc)
            except (WebSocketError, NoiseError, OSError, ValueError) as exc:
                if not self._stop.is_set():  # stop() closing the socket is not a failure
                    log.warning("connection to %s failed: %s", self.url, exc)
            except Exception:  # noqa: BLE001 - never let the thread die
                log.exception("unexpected client error")
            finally:
                self._on_disconnect()
            if self._stop.wait(backoff + random.random()):
                break
            backoff = min(backoff * 2, 30.0)

    def _on_refused(self, exc):
        legacy = self._try_legacy
        log.warning("%s (%s wire)", exc, "legacy" if legacy else "secure")
        if self.mode == MODE_AUTO and exc.before_handshake:
            self._try_legacy = not legacy  # the server may only speak the other wire
            log.info("trying the %s wire next", "legacy" if self._try_legacy else "secure")
        self.listener.on_refused(exc.reason, legacy)

    def _on_disconnect(self):
        was = self.connected
        self.connected = False
        self._ws = None
        self._reset_session()
        self.clock.reset()
        self.pipeline.end()
        if was:
            self.listener.on_disconnected()

    # -- sending -------------------------------------------------------------
    def _send(self, msg, force=False):
        ws = self._ws
        if ws is None:
            return
        self._send_on(ws, msg, force)

    def _send_on(self, ws, msg, force=False):
        text = _dumps(msg)
        if self._noise is None:
            ws.send_text(text)
            return
        if not self._send_lock.acquire(timeout=10):  # a stalled server: give up on this session
            ws.abort()
            raise WebSocketError("send timed out")
        try:
            # The nonce order must match the wire order, so encrypt and send under one lock.
            if self._quiet and not force:
                log.debug("re-handshake in progress: dropping %s", msg.get("type"))
                return
            noise = self._noise
            for plaintext in frames(TYPE_JSON, text.encode("utf-8")):
                ws.send_binary(noise.seal(plaintext))
        finally:
            self._send_lock.release()

    def _hello(self):
        player = {"supported_formats": self.formats, "buffer_capacity": self.buffer_capacity}
        payload = {
            "name": self.name,
            "supported_roles": ["player@v1", "metadata@v1", "controller@v1"],
            "device_info": {
                "product_name": self.product[:64],
                "manufacturer": "kodi-sendspin",
                "software_version": self.version,
            },
            "player@v1_support": player,
        }
        if self._legacy_wire:
            payload["client_id"] = self.client_id
            payload["version"] = PROTOCOL_VERSION
            player["supported_commands"] = LEGACY_COMMANDS
        else:
            payload["supported_pair_methods"] = {"pairing_psk": {"locations": ["operator"]}}
            payload["unpaired_access"] = {"enabled": self.unpaired_access}
        return {"type": "client/hello", "payload": payload}

    def send_state(self):
        p = self.pipeline
        player = {
            "volume": int(p.volume),
            "muted": bool(p.muted),
            "required_lead_time_ms": int(p.lead_us // 1000) + 200,
            "min_buffer_ms": int(p.lead_us // 1000) + 500,
        }
        delay_ms = int(p.static_delay_us // 1000)
        if self._legacy_wire:
            player["static_delay_ms"] = delay_ms
        else:
            player["output_delay_ms"] = max(0, min(5000, delay_ms))
            player["supported_commands"] = SECURE_COMMANDS
        self._send({"type": "client/state", "payload": {"available": bool(self.available), "player": player}})

    def send_controller(self, command, **extra):
        """Ask the server to act on our group (play/pause/stop/next/previous/...)."""
        # The controller role stays active while the group is paused (no "playback" activity).
        if not self.connected or (not self._legacy_wire and "controller@v1" not in self._roles):
            return
        payload = {"command": command}
        payload.update({k: v for k, v in extra.items() if v is not None})
        log.info("controller command %s", payload)
        self._send({"type": "client/command", "payload": {"controller": payload}})

    def set_available(self, available):
        if available == self.available:
            return
        self.available = available
        log.info("available=%s", available)
        if self.connected and self._activated:
            self.send_state()

    def set_volume(self, volume=None, muted=None, notify=True):
        if volume is not None:
            self.pipeline.volume = max(0, min(100, int(volume)))
        if muted is not None:
            self.pipeline.muted = bool(muted)
        if notify and self.connected and self._activated:
            self.send_state()

    # -- time sync -----------------------------------------------------------
    def _timesync_loop(self, ws):
        gaps = list(BURST_GAPS_S)
        while not self._stop.is_set() and not ws.closed:
            best = None
            for _ in range(BURST):
                if self._stop.is_set() or ws.closed:
                    return
                self._time_event.clear()
                tx = now_us()
                self._time_tx = tx
                try:
                    self._send_on(ws, {"type": "client/time", "payload": {"client_transmitted": tx}})
                except WebSocketError:
                    return
                if not self._time_event.wait(1.0):
                    continue  # no reply in time: send the next one anyway
                sample = self._time_reply
                if sample is not None and sample[1] > 0 and (best is None or sample[1] < best[1]):
                    best = sample
            if best is not None:
                self.clock.add_measurement(*best)
                if self._state_pending and self.clock.synced:
                    self._state_pending = False
                    try:
                        self.send_state()
                    except WebSocketError:
                        return
            if self._stop.wait(gaps.pop(0) if gaps else BURST_GAP_S):
                return

    def _on_time(self, payload, rx):
        tx = payload["client_transmitted"]
        if tx != self._time_tx:
            return  # a late reply to an earlier probe
        self._time_reply = (*measure(tx, payload["server_received"], payload["server_transmitted"], rx)[:2], rx)
        self._time_event.set()

    def _watchdog(self, ws):
        """Diagnostics: dump all thread stacks if the reader stops receiving."""
        import sys
        import traceback
        dumped = False
        while not self._stop.is_set() and not ws.closed:
            if self._stop.wait(2):
                return
            idle = (now_us() - self._last_rx) / 1e6
            if idle > 8 and not dumped:
                dumped = True
                frames_ = sys._current_frames()
                lines = ["reader idle for %.1fs - thread dump:" % idle]
                for th in threading.enumerate():
                    fr = frames_.get(th.ident)
                    if fr is not None:
                        lines.append("--- %s\n%s" % (th.name, "".join(traceback.format_stack(fr)[-6:])))
                log.warning("\n".join(lines))
            elif idle < 3:
                dumped = False

    def _start_helpers(self, ws):
        threading.Thread(target=self._timesync_loop, args=(ws,), name="sendspin-time", daemon=True).start()
        self._last_rx = now_us()
        threading.Thread(target=self._watchdog, args=(ws,), name="sendspin-watchdog", daemon=True).start()

    # -- session ---------------------------------------------------------------
    def _session(self):
        ws = WebSocket(self.url)
        ws.connect()
        try:
            self._ws = ws
            if self._try_legacy:
                log.info("connected to %s (legacy wire)", self.url)
                self._send(self._hello())
                self._start_helpers(ws)
            else:
                log.info("connected to %s (secure wire)", self.url)
                self._secure_handshake(ws)
            while not self._stop.is_set():
                op, data = ws.recv()
                rx = now_us()
                self._last_rx = rx
                if self._noise is not None:
                    if op != OP_BINARY:
                        raise ProtocolError("text frame after the handshake")
                    got = self._reasm.feed(self._noise.open(data))
                    if got is None:
                        continue
                    msg_type, body = got
                    if msg_type == TYPE_JSON:
                        self._on_text(self._parse(body), rx)
                    elif msg_type == BIN_AUDIO_CHUNK:
                        self._on_binary(bytes([msg_type]) + body)
                    # other binary types belong to roles we do not have: ignore
                elif op == OP_BINARY:
                    self._on_binary(data)
                elif op == OP_TEXT:
                    self._on_text(self._parse(data), rx)
        finally:
            ws.close()  # ends this session's time and watchdog threads before any reconnect

    @staticmethod
    def _parse(data):
        msg = json.loads(data)
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ProtocolError("message is not a JSON object with a type")
        if not isinstance(msg.get("payload", {}), dict):
            raise ProtocolError("payload is not an object")
        return msg

    def _recv_text(self, ws):
        op, data = ws.recv()
        if op != OP_TEXT:
            raise ServerRefused("binary frame during the handshake")
        return data

    def _secure_handshake(self, ws):
        """client/init -> server/init -> Noise messages 1 and 2. Leaves self._noise set."""
        done = threading.Event()
        timer = threading.Timer(HANDSHAKE_TIMEOUT_S, lambda: None if done.is_set() else ws.abort())
        timer.daemon = True
        timer.start()
        try:
            ident = self.identity
            init_text = _dumps({"type": "client/init",
                                "payload": {"client_id": ident.client_id, "version": PROTOCOL_VERSION,
                                            "suite": SUITE}})
            ws.send_text(init_text)
            try:
                server_init_text = self._recv_text(ws)
            except WebSocketError as exc:
                if self._stop.is_set() or done.is_set():
                    raise
                # closed straight away: most likely a server that only speaks the legacy wire
                raise ServerRefused("closed before server/init (%s)" % exc) from exc
            msg = self._parse(server_init_text)
            payload = msg.get("payload") or {}
            if msg["type"] == "server/error":
                raise ServerRefused(str(payload.get("reason") or "unknown"))
            if msg["type"] != "server/init":
                raise ServerRefused("unexpected %s instead of server/init" % msg["type"])
            version = payload.get("version")
            if type(version) is not int or version != PROTOCOL_VERSION:
                raise ServerRefused("unsupported protocol version %r" % (version,))
            server_id = payload.get("server_id")
            server_pub = b64u_decode(server_id, 32)
            hs = self._parse(self._recv_text(ws))
            if hs["type"] != "noise/handshake":
                raise ProtocolError("expected noise/handshake, got %s" % hs["type"])
            responder = Responder(ident.private, ident.public, server_pub,
                                  init_text.encode("utf-8") + server_init_text.encode("utf-8"))
            payload1 = responder.read_message1(b64u_decode((hs.get("payload") or {}).get("data")))
            psk, category = ident.resolve_psk(payload1, server_id, allow_sentinel_fallback=True)
            message2, transport = responder.write_message2(psk)
            ws.send_text(_dumps({"type": "noise/handshake", "payload": {"data": b64u_encode(message2)}}))
            self._noise, self._legacy_wire = transport, False
            self._server_id, self._psk_category = server_id, category
            log.info("encrypted session up (%s)", {"lt": "paired", "pr": "pairing key", "sn": "unpaired"}[category])
            done.set()
        finally:
            timer.cancel()

    def _rehandshake(self, payload):
        """An in-band noise/handshake: swap to new keys, e.g. after pairing."""
        ident = self.identity
        self._quiet = True  # no new application messages until the next server/activate
        responder = Responder(ident.private, ident.public, b64u_decode(self._server_id, 32),
                              self._noise.handshake_hash)
        payload1 = responder.read_message1(b64u_decode(payload.get("data")))
        try:
            psk, category = ident.resolve_psk(payload1, self._server_id, allow_sentinel_fallback=False)
        except ValueError as exc:
            raise ProtocolError("re-handshake: %s" % exc) from exc
        message2, transport = responder.write_message2(psk)
        ws = self._ws
        with self._send_lock:
            noise = self._noise
            reply = _dumps({"type": "noise/handshake", "payload": {"data": b64u_encode(message2)}})
            for plaintext in frames(TYPE_JSON, reply.encode("utf-8")):
                ws.send_binary(noise.seal(plaintext))  # still under the old keys
            self._noise = transport
        self._psk_category = category
        self._pairing_index = 0
        self._pending_psk = None
        log.info("re-handshake done (%s)", {"lt": "paired", "pr": "pairing key", "sn": "unpaired"}[category])

    # -- receiving -------------------------------------------------------------
    def _on_binary(self, data):
        if not data or data[0] != BIN_AUDIO_CHUNK:
            return
        if not self._legacy_wire and not self._playback:
            return  # audio only counts while "playback" is active
        header = 9 if self._legacy_wire else 13  # the secure wire adds a uint32 send_ahead
        if len(data) < header:
            if not self._legacy_wire:
                raise ProtocolError("short audio chunk")
            return
        ts = struct.unpack(">q", data[1:9])[0]
        pcm = data[header:]
        fmt = getattr(self.pipeline, "fmt", None)
        if fmt is not None and len(pcm) % fmt.frame_bytes:
            return  # not whole sample frames: not audio we can play
        self.pipeline.push(ts, pcm)

    def _accept_format(self, player):
        """The stream format, if it is one we offered in client/hello; otherwise None."""
        chosen = {k: player.get(k) for k in ("codec", "channels", "sample_rate", "bit_depth")}
        if chosen not in self.formats:
            return None
        return AudioFormat(chosen["sample_rate"], chosen["channels"], chosen["bit_depth"])

    def _on_text(self, msg, rx):
        typ = msg.get("type")
        payload = msg.get("payload") or {}
        if typ == "server/time":
            self._on_time(payload, rx)
        elif typ == "server/hello":
            self.server_name = payload.get("name")
            self.connected = True
            if self._legacy_wire:
                log.info("server hello: %s roles=%s", self.server_name, payload.get("active_roles"))
                self._activated = self._playback = True
                self.send_state()
                self.listener.on_connected(self.server_name)
            else:
                log.info("server hello: %s", self.server_name)
                self._send(self._hello())
        elif typ == "server/activate" and not self._legacy_wire:
            self._on_activate(payload)
        elif typ == "noise/handshake" and not self._legacy_wire:
            self._rehandshake(payload)
        elif typ == "server/error":
            raise ServerRefused(str(payload.get("reason") or "unknown"), before_handshake=not self.connected)
        elif typ == "server/pair-finalize" and not self._legacy_wire:
            self._on_pair_finalize()
        elif typ == "pair/abort":
            log.warning("pairing aborted by the server: %s", payload.get("reason"))
            self._pending_psk = None
        elif typ == "server/unpair" and not self._legacy_wire:
            self._on_unpair()
        elif typ == "stream/start":
            player = payload.get("player")
            if player and (self._legacy_wire or self._playback):
                fmt = self._accept_format(player)
                if fmt is None:
                    log.error("unsupported stream format %s", player)
                    self.pipeline.end()
                    self.listener.on_stream_end()
                    return
                gen, continuous = self.pipeline.start(fmt, continuous=self.listener.output_running())
                self.listener.on_stream_start(fmt, gen, continuous)
        elif typ == "stream/clear":
            roles = payload.get("roles")
            if roles is None or "player" in roles:
                gen = self.pipeline.clear(payload.get("from_timestamp"))
                self.listener.on_stream_clear(gen)
        elif typ == "stream/end":
            roles = payload.get("roles")
            if roles is None or "player" in roles:
                self.pipeline.end()
                self.listener.on_stream_end()
        elif typ == "server/state":
            md = payload.get("metadata")
            if isinstance(md, dict):
                if self._legacy_wire:
                    self.metadata.update(md)  # legacy: partial updates
                else:
                    self.metadata = dict(md)  # secure: full state every time
                self.listener.on_metadata(dict(self.metadata))
        elif typ == "server/command":
            self._on_command(payload.get("player") or {})
        elif typ == "group/update":
            # The spec wants a 43-char base64url group_id, but Music Assistant 2.10 sends a UUID.
            # We only pass it on, so any value is fine.
            self.listener.on_group(payload)
        else:
            log.debug("unhandled message %s", typ)

    def _close_with(self, reason):
        log.warning("closing: %s", reason)
        self._send({"type": "client/goodbye", "payload": {"reason": reason}}, force=True)
        raise ProtocolError("goodbye %s" % reason)

    def _on_activate(self, payload):
        activities = payload.get("activities")
        if not isinstance(activities, list) or any(a not in ACTIVITIES for a in activities) \
                or len(set(activities)) != len(activities):
            self._close_with("unauthorized")
        roles = payload.get("active_roles")
        paired = self._psk_category == LONG_TERM
        acts = set(activities)
        if paired:
            allowed = acts <= {"playback"}
        else:
            allowed = "playback" not in acts or self.unpaired_access
        if not allowed:
            self._close_with("pairing_required" if not paired and acts <= set(ACTIVITIES) else "unauthorized")
        playback_capable = paired or self.unpaired_access
        if roles is not None and roles and not playback_capable:
            self._close_with("unauthorized")
        if roles is not None:
            self._roles = list(roles)
        elif not playback_capable:
            self._roles = []
        self._quiet = False
        first = not self._activated
        self._activated = True
        was_playing = self._playback
        self._playback = "playback" in acts and playback_capable
        log.info("activated: activities=%s roles=%s", activities, roles)
        if was_playing and not self._playback:
            self.pipeline.end()
            self.listener.on_stream_end()
        if self._pending_psk is not None:
            log.info("pairing cancelled by the server")
            self._pending_psk = None
        if "pairing" in acts:
            self._start_pairing(payload.get("pairing") or {})
        if first:
            self._start_helpers(self._ws)
            self.listener.on_connected(self.server_name)
        if self.clock.synced:
            self.send_state()
        else:
            self._state_pending = True  # the time thread sends it after the first burst

    # -- pairing -----------------------------------------------------------------
    def _start_pairing(self, pairing):
        self._pairing_index += 1
        if pairing.get("method") != "pairing_psk" or self._psk_category != PAIRING:
            log.warning("server asked for pairing method %r - not supported", pairing.get("method"))
            self._send({"type": "pair/abort", "payload": {"reason": "method_not_supported"}})
            return
        psk = os.urandom(32)
        self._pending_psk = psk
        log.info("pairing with %s", self.server_name)
        self._send({"type": "client/pair-init", "payload": {"pairing_index": self._pairing_index}})
        self._send({"type": "client/pair-finalize", "payload": {"long_term_psk": b64u_encode(psk)}})

    def _on_pair_finalize(self):
        psk, self._pending_psk = self._pending_psk, None
        if psk is None:
            return  # a leftover from an aborted attempt
        self.identity.add_pairing(self._server_id, psk, self.server_name)
        log.info("paired with %s", self.server_name)
        self.listener.on_paired(self.server_name)

    def _on_unpair(self):
        if self._psk_category != LONG_TERM:
            return  # unpaired session: nothing to remove
        self.identity.remove_pairing(self._server_id)
        log.info("%s removed its pairing", self.server_name)
        self.listener.on_unpaired(self.server_name)
        self._close_with("unpaired")

    def _on_command(self, cmd):
        name = cmd.get("command")
        if name == "volume" and cmd.get("volume") is not None:
            self.set_volume(volume=cmd["volume"])
            self.listener.on_volume(self.pipeline.volume, self.pipeline.muted)
        elif name == "mute" and cmd.get("mute") is not None:
            self.set_volume(muted=cmd["mute"])
            self.listener.on_volume(self.pipeline.volume, self.pipeline.muted)
        elif name in ("set_output_delay", "set_static_delay"):
            delay = cmd.get("output_delay_ms", cmd.get("static_delay_ms"))
            if delay is None:
                return
            self.pipeline.static_delay_us = int(delay) * 1000
            self.listener.on_static_delay(int(delay))
            self.send_state()
        else:
            log.debug("unhandled command %s", cmd)
