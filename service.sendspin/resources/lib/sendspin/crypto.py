"""X25519 and ChaCha20-Poly1305 for Sendspin's Noise channel, standard library only.

Kodi on Android cannot install compiled packages, so everything has a pure-Python
version. Faster backends are used when available, in this order:

1. the ``cryptography`` package (desktop Pythons);
2. OpenSSL's libcrypto through ``ctypes`` (Kodi ships it for ``ssl`` / ``hashlib``);
3. pure Python. ChaCha20 runs 64-bit "lanes" inside big integers, so one Python
   operation works on many blocks at once (a few MB/s; PCM needs about 0.2 MB/s).

Every backend is checked against the RFC 8439 test vector before it is used.
Set ``SENDSPIN_CRYPTO=pure`` to force the pure-Python code (tests, debugging).
"""

import base64
import hmac
import logging
import os
import struct
import sys

log = logging.getLogger("sendspin.crypto")


class CryptoError(Exception):
    """Authentication failed or bad input."""


def b64u_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text, length=None):
    if not isinstance(text, str) or "=" in text:
        raise ValueError("bad base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u_encode(data) != text:
        raise ValueError("non-canonical base64url")
    if length is not None and len(data) != length:
        raise ValueError("expected %d bytes, got %d" % (length, len(data)))
    return data


# -- X25519 (RFC 7748) -------------------------------------------------------

_P = 2 ** 255 - 19
_A24 = 121665


def _x25519_pure(scalar, u):
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = x2 + z2, x2 - z2
        aa, bb = a * a % _P, b * b % _P
        e = aa - bb
        c, d = x3 + z3, x3 - z3
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE = (9).to_bytes(32, "little")


def x25519(private, public):
    """Shared secret. Raises CryptoError for a low-order public key (all-zero result)."""
    if len(private) != 32 or len(public) != 32:
        raise CryptoError("X25519 keys are 32 bytes")
    out = _x25519_pure(private, public)
    if out == bytes(32):
        raise CryptoError("low-order X25519 public key")
    return out


def x25519_public(private):
    return _x25519_pure(private, _BASE)


def x25519_keypair():
    private = os.urandom(32)
    return private, x25519_public(private)


# -- ChaCha20-Poly1305 (RFC 8439), pure Python --------------------------------

_LANE = 8  # bytes per lane: 32 data bits + 32 guard bits so additions never spill
_ROUND_TO = 8  # keystream is made in multiples of this many blocks (caps the cache size)
_lane_cache = {}


def _lanes(n):
    """(mask, repeat-1, 0..n-1) as lane integers for ``n`` blocks."""
    got = _lane_cache.get(n)
    if got is None:
        one = b"\x01" + b"\x00" * (_LANE - 1)
        rep = int.from_bytes(one * n, "little")
        mask = rep * 0xFFFFFFFF
        index = int.from_bytes(b"".join(struct.pack("<Q", i) for i in range(n)), "little")
        got = _lane_cache[n] = (mask, rep, index)
    return got


def _keystream(key, nonce, counter, nbytes):
    """ChaCha20 keystream for blocks ``counter``, ``counter+1``... (at least ``nbytes``)."""
    n = -(-max(nbytes, 1) // 64)
    n = -(-n // _ROUND_TO) * _ROUND_TO
    mask, rep, index = _lanes(n)
    words = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574) + struct.unpack("<8I", key)
    init = [w * rep for w in words]
    init.append((index + counter * rep) & mask)  # 32-bit counter wraps per lane
    init += [w * rep for w in struct.unpack("<3I", nonce)]
    x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15 = init
    m = mask
    for _ in range(10):
        # column rounds
        x0 = (x0 + x4) & m; x12 ^= x0; x12 = ((x12 << 16) | (x12 >> 16)) & m  # noqa: E702
        x8 = (x8 + x12) & m; x4 ^= x8; x4 = ((x4 << 12) | (x4 >> 20)) & m  # noqa: E702
        x0 = (x0 + x4) & m; x12 ^= x0; x12 = ((x12 << 8) | (x12 >> 24)) & m  # noqa: E702
        x8 = (x8 + x12) & m; x4 ^= x8; x4 = ((x4 << 7) | (x4 >> 25)) & m  # noqa: E702
        x1 = (x1 + x5) & m; x13 ^= x1; x13 = ((x13 << 16) | (x13 >> 16)) & m  # noqa: E702
        x9 = (x9 + x13) & m; x5 ^= x9; x5 = ((x5 << 12) | (x5 >> 20)) & m  # noqa: E702
        x1 = (x1 + x5) & m; x13 ^= x1; x13 = ((x13 << 8) | (x13 >> 24)) & m  # noqa: E702
        x9 = (x9 + x13) & m; x5 ^= x9; x5 = ((x5 << 7) | (x5 >> 25)) & m  # noqa: E702
        x2 = (x2 + x6) & m; x14 ^= x2; x14 = ((x14 << 16) | (x14 >> 16)) & m  # noqa: E702
        x10 = (x10 + x14) & m; x6 ^= x10; x6 = ((x6 << 12) | (x6 >> 20)) & m  # noqa: E702
        x2 = (x2 + x6) & m; x14 ^= x2; x14 = ((x14 << 8) | (x14 >> 24)) & m  # noqa: E702
        x10 = (x10 + x14) & m; x6 ^= x10; x6 = ((x6 << 7) | (x6 >> 25)) & m  # noqa: E702
        x3 = (x3 + x7) & m; x15 ^= x3; x15 = ((x15 << 16) | (x15 >> 16)) & m  # noqa: E702
        x11 = (x11 + x15) & m; x7 ^= x11; x7 = ((x7 << 12) | (x7 >> 20)) & m  # noqa: E702
        x3 = (x3 + x7) & m; x15 ^= x3; x15 = ((x15 << 8) | (x15 >> 24)) & m  # noqa: E702
        x11 = (x11 + x15) & m; x7 ^= x11; x7 = ((x7 << 7) | (x7 >> 25)) & m  # noqa: E702
        # diagonal rounds
        x0 = (x0 + x5) & m; x15 ^= x0; x15 = ((x15 << 16) | (x15 >> 16)) & m  # noqa: E702
        x10 = (x10 + x15) & m; x5 ^= x10; x5 = ((x5 << 12) | (x5 >> 20)) & m  # noqa: E702
        x0 = (x0 + x5) & m; x15 ^= x0; x15 = ((x15 << 8) | (x15 >> 24)) & m  # noqa: E702
        x10 = (x10 + x15) & m; x5 ^= x10; x5 = ((x5 << 7) | (x5 >> 25)) & m  # noqa: E702
        x1 = (x1 + x6) & m; x12 ^= x1; x12 = ((x12 << 16) | (x12 >> 16)) & m  # noqa: E702
        x11 = (x11 + x12) & m; x6 ^= x11; x6 = ((x6 << 12) | (x6 >> 20)) & m  # noqa: E702
        x1 = (x1 + x6) & m; x12 ^= x1; x12 = ((x12 << 8) | (x12 >> 24)) & m  # noqa: E702
        x11 = (x11 + x12) & m; x6 ^= x11; x6 = ((x6 << 7) | (x6 >> 25)) & m  # noqa: E702
        x2 = (x2 + x7) & m; x13 ^= x2; x13 = ((x13 << 16) | (x13 >> 16)) & m  # noqa: E702
        x8 = (x8 + x13) & m; x7 ^= x8; x7 = ((x7 << 12) | (x7 >> 20)) & m  # noqa: E702
        x2 = (x2 + x7) & m; x13 ^= x2; x13 = ((x13 << 8) | (x13 >> 24)) & m  # noqa: E702
        x8 = (x8 + x13) & m; x7 ^= x8; x7 = ((x7 << 7) | (x7 >> 25)) & m  # noqa: E702
        x3 = (x3 + x4) & m; x14 ^= x3; x14 = ((x14 << 16) | (x14 >> 16)) & m  # noqa: E702
        x9 = (x9 + x14) & m; x4 ^= x9; x4 = ((x4 << 12) | (x4 >> 20)) & m  # noqa: E702
        x3 = (x3 + x4) & m; x14 ^= x3; x14 = ((x14 << 8) | (x14 >> 24)) & m  # noqa: E702
        x9 = (x9 + x14) & m; x4 ^= x9; x4 = ((x4 << 7) | (x4 >> 25)) & m  # noqa: E702
    out = bytearray(64 * n)
    for w, (x, x_init) in enumerate(zip((x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15),
                                        init)):
        lane_bytes = ((x + x_init) & m).to_bytes(_LANE * n, "little")
        for b in range(4):  # word w is bytes 4w..4w+3 of every 64-byte block
            out[4 * w + b::64] = lane_bytes[b::_LANE]
    return out


def _xor(data, stream):
    n = len(data)
    if not n:
        return b""
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream[:n], "little")).to_bytes(n, "little")


_P1305 = (1 << 130) - 5


def _poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    acc = 0
    full = len(msg) - len(msg) % 16
    hibit = 1 << 128
    for i in range(0, full, 16):
        acc = (acc + int.from_bytes(msg[i:i + 16], "little") + hibit) * r % _P1305
    if full < len(msg):
        tail = msg[full:]
        acc = (acc + int.from_bytes(tail, "little") + (1 << (8 * len(tail)))) * r % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(n):
    return bytes(-n % 16)


def _mac_data(ad, ct):
    return ad + _pad16(len(ad)) + ct + _pad16(len(ct)) + struct.pack("<QQ", len(ad), len(ct))


class _PureChaChaPoly:
    name = "pure-python"

    def __init__(self, key):
        self._key = bytes(key)

    def encrypt(self, nonce, plaintext, ad):
        stream = _keystream(self._key, nonce, 0, 64 + len(plaintext))
        ct = _xor(plaintext, memoryview(stream)[64:])
        return ct + _poly1305(stream[:32], _mac_data(ad, ct))

    def decrypt(self, nonce, data, ad):
        if len(data) < 16:
            raise CryptoError("ciphertext too short")
        ct, tag = bytes(data[:-16]), bytes(data[-16:])
        stream = _keystream(self._key, nonce, 0, 64 + len(ct))
        if not hmac.compare_digest(_poly1305(stream[:32], _mac_data(ad, ct)), tag):
            raise CryptoError("authentication failed")
        return _xor(ct, memoryview(stream)[64:])


# -- faster backends ----------------------------------------------------------

def _cryptography_backend():
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: PLC0415

    class _Lib:
        name = "cryptography"

        def __init__(self, key):
            self._aead = ChaCha20Poly1305(bytes(key))

        def encrypt(self, nonce, plaintext, ad):
            return self._aead.encrypt(nonce, bytes(plaintext), ad)

        def decrypt(self, nonce, data, ad):
            try:
                return self._aead.decrypt(nonce, bytes(data), ad)
            except InvalidTag as exc:
                raise CryptoError("authentication failed") from exc

    return _Lib


def _libcrypto_candidates():
    """Handles that may expose libcrypto symbols. Never asks macOS for its system libcrypto
    (dlopen-ing that one aborts the process); Python's own _hashlib links its bundled copy."""
    import ctypes  # noqa: PLC0415
    try:
        import _hashlib  # noqa: PLC0415
        yield ctypes.CDLL(_hashlib.__file__)  # dlsym also searches its dependency, libcrypto
    except (ImportError, OSError, AttributeError):
        pass
    if sys.platform.startswith("win"):
        names = ["libcrypto-3-x64", "libcrypto-3", "libcrypto-1_1-x64", "libcrypto-1_1"]
    elif sys.platform == "darwin":
        names = []
    else:
        names = ["libcrypto.so", "libcrypto.so.3", "libcrypto.so.1.1"]
    for name in names:
        try:
            yield ctypes.CDLL(name)
        except OSError:
            continue


def _libcrypto_backend():
    import ctypes  # noqa: PLC0415
    lib = None
    for cand in _libcrypto_candidates():
        try:
            cand.EVP_chacha20_poly1305
            cand.EVP_CIPHER_CTX_new
        except AttributeError:
            continue
        lib = cand
        break
    if lib is None:
        raise OSError("libcrypto with ChaCha20-Poly1305 not found")

    vp, ip, cp = ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p
    lib.EVP_chacha20_poly1305.restype = vp
    lib.EVP_chacha20_poly1305.argtypes = []
    lib.EVP_CIPHER_CTX_new.restype = vp
    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_CIPHER_CTX_free.argtypes = [vp]
    lib.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int
    lib.EVP_CIPHER_CTX_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp]
    for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
        getattr(lib, fn).restype = ctypes.c_int
        getattr(lib, fn).argtypes = [vp, vp, vp, cp, cp]
    for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
        getattr(lib, fn).restype = ctypes.c_int
        getattr(lib, fn).argtypes = [vp, vp, ip, cp, ctypes.c_int]
    for fn in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
        getattr(lib, fn).restype = ctypes.c_int
        getattr(lib, fn).argtypes = [vp, vp, ip]
    cipher = lib.EVP_chacha20_poly1305()
    if not cipher:
        raise OSError("EVP_chacha20_poly1305 unavailable")
    set_ivlen, get_tag, set_tag = 0x9, 0x10, 0x11

    class _Lib:
        name = "libcrypto"

        def __init__(self, key):
            self._key = bytes(key)

        def _run(self, encrypt, nonce, data, ad, tag=None):
            init, update, final = ((lib.EVP_EncryptInit_ex, lib.EVP_EncryptUpdate, lib.EVP_EncryptFinal_ex)
                                   if encrypt else
                                   (lib.EVP_DecryptInit_ex, lib.EVP_DecryptUpdate, lib.EVP_DecryptFinal_ex))
            ctx = lib.EVP_CIPHER_CTX_new()
            if not ctx:
                raise MemoryError("EVP_CIPHER_CTX_new")
            try:
                n = ctypes.c_int(0)
                out = ctypes.create_string_buffer(len(data) + 16)
                ok = init(ctx, cipher, None, None, None) == 1
                ok = ok and lib.EVP_CIPHER_CTX_ctrl(ctx, set_ivlen, 12, None) == 1
                ok = ok and init(ctx, None, None, self._key, bytes(nonce)) == 1
                if ok and ad:
                    ok = update(ctx, None, ctypes.byref(n), ad, len(ad)) == 1
                ok = ok and update(ctx, out, ctypes.byref(n), bytes(data), len(data)) == 1
                written = n.value
                if not ok:
                    raise CryptoError("libcrypto failure")
                if tag is not None:
                    tag_buf = ctypes.create_string_buffer(bytes(tag), 16)
                    if lib.EVP_CIPHER_CTX_ctrl(ctx, set_tag, 16, ctypes.cast(tag_buf, vp)) != 1:
                        raise CryptoError("libcrypto failure")
                tail = ctypes.c_int(0)
                if final(ctx, ctypes.byref(out, written), ctypes.byref(tail)) != 1:
                    raise CryptoError("authentication failed")
                result = out.raw[:written + tail.value]
                if encrypt:
                    tag_buf = ctypes.create_string_buffer(16)
                    if lib.EVP_CIPHER_CTX_ctrl(ctx, get_tag, 16, ctypes.cast(tag_buf, vp)) != 1:
                        raise CryptoError("libcrypto failure")
                    result += tag_buf.raw
                return result
            finally:
                lib.EVP_CIPHER_CTX_free(ctx)

        def encrypt(self, nonce, plaintext, ad):
            return self._run(True, nonce, plaintext, ad)

        def decrypt(self, nonce, data, ad):
            if len(data) < 16:
                raise CryptoError("ciphertext too short")
            return self._run(False, nonce, data[:-16], ad, tag=data[-16:])

    return _Lib


# RFC 8439 section 2.8.2
_TV_KEY = bytes(range(0x80, 0xA0))
_TV_NONCE = bytes.fromhex("070000004041424344454647")
_TV_AD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
_TV_PT = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for "
          b"the future, sunscreen would be it.")
_TV_TAG = bytes.fromhex("1ae10b594f09e26a7e902ecbd0600691")


def _self_test(cls):
    aead = cls(_TV_KEY)
    sealed = aead.encrypt(_TV_NONCE, _TV_PT, _TV_AD)
    if sealed[-16:] != _TV_TAG or aead.decrypt(_TV_NONCE, sealed, _TV_AD) != _TV_PT:
        raise CryptoError("%s failed the RFC 8439 test vector" % cls.name)
    try:
        aead.decrypt(_TV_NONCE, sealed[:-1] + bytes([sealed[-1] ^ 1]), _TV_AD)
    except CryptoError:
        return cls
    raise CryptoError("%s accepted a forged tag" % cls.name)


def _pick_backend():
    if os.environ.get("SENDSPIN_CRYPTO") != "pure":
        for make in (_cryptography_backend, _libcrypto_backend):
            try:
                return _self_test(make())
            except Exception as exc:  # noqa: BLE001 - any failure: try the next backend
                log.debug("crypto backend %s unavailable: %s", make.__name__, exc)
    return _self_test(_PureChaChaPoly)


ChaChaPoly = _pick_backend()
PureChaChaPoly = _PureChaChaPoly
