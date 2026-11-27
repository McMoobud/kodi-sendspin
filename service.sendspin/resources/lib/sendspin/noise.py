"""Noise ``KKpsk2_25519_ChaChaPoly_SHA256`` as the Sendspin client (Noise responder).

The server is always the initiator. Both sides know each other's static key up front
(``server_id`` / ``client_id`` are the base64url public keys). Message 1 carries a JSON
payload naming the PSK to use; message 2 mixes it in and carries ``{}``.

After the handshake every WebSocket binary frame is one encrypted message whose
first plaintext byte is its type (0 JSON, 1 fragment, 4 audio, ...).
"""

import hashlib
import hmac
import struct

from .crypto import ChaChaPoly, CryptoError, x25519, x25519_keypair

PROTOCOL_NAME = b"Noise_KKpsk2_25519_ChaChaPoly_SHA256"
SUITE = "25519_ChaChaPoly_SHA256"
DH_LEN = 32
TAG_LEN = 16
MAX_MESSAGE = 65535
MAX_PLAINTEXT = MAX_MESSAGE - TAG_LEN  # type byte + body

TYPE_JSON = 0
TYPE_FRAGMENT = 1
FRAG_LAST = 0x01
FRAG_FIRST = 0x02
FRAG_RESERVED = 0xFC
MAX_REASSEMBLED = 16 * 1024 * 1024  # artwork is the biggest thing a player gets


class NoiseError(Exception):
    """Handshake or transport failure: the connection must be closed."""


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def hkdf(ck, ikm, n):
    tk = _hmac(ck, ikm)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = _hmac(tk, prev + bytes([i]))
        out.append(prev)
    return out


class CipherState:
    """One direction: key + 64-bit counter. ChaChaPoly nonce = 4 zero bytes + little-endian counter."""

    def __init__(self, key=None):
        self._aead = ChaChaPoly(key) if key is not None else None
        self.n = 0

    @property
    def has_key(self):
        return self._aead is not None

    def _nonce(self):
        if self.n >= 2 ** 64 - 1:
            raise NoiseError("nonce exhausted")
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", self.n)
        self.n += 1
        return nonce

    def encrypt(self, ad, plaintext):
        if self._aead is None:
            return bytes(plaintext)
        return self._aead.encrypt(self._nonce(), plaintext, ad)

    def decrypt(self, ad, ciphertext):
        if self._aead is None:
            return bytes(ciphertext)
        try:
            return self._aead.decrypt(self._nonce(), ciphertext, ad)
        except CryptoError as exc:
            raise NoiseError("decryption failed") from exc


class Responder:
    """Runs the client side of one handshake. ``prologue`` is client/init + server/init text
    for the first handshake, or the previous handshake hash for a re-handshake."""

    def __init__(self, static_private, static_public, server_public, prologue):
        self._s_priv, self._s_pub, self._rs = static_private, static_public, server_public
        self.h = hashlib.sha256(PROTOCOL_NAME).digest() if len(PROTOCOL_NAME) > 32 \
            else PROTOCOL_NAME.ljust(32, b"\x00")
        self._ck = self.h
        self._cipher = CipherState()
        self._re = None
        self._mix_hash(prologue)
        self._mix_hash(server_public)  # initiator pre-message first
        self._mix_hash(static_public)

    def _mix_hash(self, data):
        self.h = hashlib.sha256(self.h + data).digest()

    def _mix_key(self, ikm):
        self._ck, key = hkdf(self._ck, ikm, 2)
        self._cipher = CipherState(key)

    def _encrypt_and_hash(self, plaintext):
        out = self._cipher.encrypt(self.h, plaintext)
        self._mix_hash(out)
        return out

    def _decrypt_and_hash(self, data):
        out = self._cipher.decrypt(self.h, data)
        self._mix_hash(data)
        return out

    def read_message1(self, message):
        """-> e, es, ss. Returns the decrypted payload (names the PSK)."""
        if len(message) < DH_LEN + TAG_LEN:
            raise NoiseError("handshake message 1 too short")
        self._re = bytes(message[:DH_LEN])
        self._mix_hash(self._re)
        self._mix_key(self._re)  # psk handshakes also mix e into the key
        try:
            self._mix_key(x25519(self._s_priv, self._re))  # es
            self._mix_key(x25519(self._s_priv, self._rs))  # ss
        except CryptoError as exc:
            raise NoiseError(str(exc)) from exc
        return self._decrypt_and_hash(message[DH_LEN:])

    def write_message2(self, psk, payload=b"{}"):
        """<- e, ee, se, psk. Returns (message bytes, Transport)."""
        if self._re is None:
            raise NoiseError("message 1 not read")
        e_priv, e_pub = x25519_keypair()
        self._mix_hash(e_pub)
        self._mix_key(e_pub)
        try:
            self._mix_key(x25519(e_priv, self._re))  # ee
            self._mix_key(x25519(e_priv, self._rs))  # se (responder: DH(e, rs))
        except CryptoError as exc:
            raise NoiseError(str(exc)) from exc
        self._ck, temp_h, temp_k = hkdf(self._ck, psk, 3)  # MixKeyAndHash(psk)
        self._mix_hash(temp_h)
        self._cipher = CipherState(temp_k)
        message = e_pub + self._encrypt_and_hash(payload)
        k1, k2 = hkdf(self._ck, b"", 2)
        # The initiator (server) sends with k1, so we receive with k1 and send with k2.
        return message, Transport(send=CipherState(k2), recv=CipherState(k1), handshake_hash=self.h)


class Transport:
    def __init__(self, send, recv, handshake_hash):
        self.send = send
        self.recv = recv
        self.handshake_hash = handshake_hash

    def seal(self, plaintext):
        if not plaintext or len(plaintext) > MAX_PLAINTEXT:
            raise NoiseError("bad transport plaintext length %d" % len(plaintext))
        return self.send.encrypt(b"", plaintext)

    def open(self, data):
        if len(data) > MAX_MESSAGE:
            raise NoiseError("transport message too large")
        plaintext = self.recv.decrypt(b"", data)
        if not plaintext:
            raise NoiseError("empty transport message")
        return plaintext


def frames(msg_type, body):
    """Plaintexts for one message: a single frame, or type-1 fragments if it is too big."""
    if len(body) + 1 <= MAX_PLAINTEXT:
        return [bytes([msg_type]) + body]
    out, pos = [], 0
    first_room = MAX_PLAINTEXT - 3
    room = MAX_PLAINTEXT - 2
    while pos < len(body):
        first = pos == 0
        size = first_room if first else room
        chunk = body[pos:pos + size]
        pos += len(chunk)
        flags = (FRAG_FIRST if first else 0) | (FRAG_LAST if pos >= len(body) else 0)
        head = bytes([TYPE_FRAGMENT, flags]) + (bytes([msg_type]) if first else b"")
        out.append(head + chunk)
    return out


class Reassembler:
    """Feeds plaintexts in; returns (type, body) for complete messages, None while fragmented."""

    def __init__(self):
        self._type = None
        self._parts = []
        self._size = 0

    def feed(self, plaintext):
        msg_type = plaintext[0]
        if msg_type != TYPE_FRAGMENT:
            if self._type is not None:
                raise NoiseError("message interleaved with a fragmented one")
            if msg_type in (2, 3):
                raise NoiseError("reserved message type %d" % msg_type)
            return msg_type, bytes(plaintext[1:])
        if len(plaintext) < 2:
            raise NoiseError("fragment without flags")
        flags = plaintext[1]
        if flags & FRAG_RESERVED:
            raise NoiseError("reserved fragment flags")
        if flags & FRAG_FIRST:
            if self._type is not None or len(plaintext) < 3 or plaintext[2] == TYPE_FRAGMENT:
                raise NoiseError("bad first fragment")
            self._type, data = plaintext[2], plaintext[3:]
            self._parts, self._size = [], 0
        else:
            if self._type is None:
                raise NoiseError("stray continuation fragment")
            data = plaintext[2:]
        self._size += len(data)
        if self._size > MAX_REASSEMBLED:
            raise NoiseError("fragmented message too large")
        self._parts.append(bytes(data))
        if not flags & FRAG_LAST:
            return None
        msg_type, body = self._type, b"".join(self._parts)
        self._type, self._parts, self._size = None, [], 0
        return msg_type, body
