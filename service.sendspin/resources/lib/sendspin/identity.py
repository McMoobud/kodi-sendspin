"""This player's Sendspin identity and its pairings, stored as JSON in one folder.

- ``identity.json``: the X25519 keypair (its public key is the ``client_id``) and the
  pairing PSK. The pairing PSK is what the pairing token hands to the server operator.
- ``pairings.json``: one record per paired server: ``server_id`` + long-term PSK.

Files are re-read on every use, so a separate process (the settings action script)
can forget pairings while the service runs.
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time

from .crypto import b64u_decode, b64u_encode, x25519_public

log = logging.getLogger("sendspin.identity")

SENTINEL_PSK = hashlib.sha256(b"sendspin-sentinel-psk-v1").digest()
MAX_PAIRINGS = 8  # the spec asks for at least 5

LONG_TERM, PAIRING, SENTINEL = "lt", "pr", "sn"


def psk_id(psk):
    return b64u_encode(hashlib.sha256(b"sendspin-psk-id-v1" + psk).digest())


SENTINEL_ID = psk_id(SENTINEL_PSK)


def pairing_token(public_key, pairing_psk):
    """``SP:0`` + base32(pubkey || pairing PSK), no padding, '2' shown as '9' (easier to read)."""
    text = base64.b32encode(public_key + pairing_psk).decode("ascii").rstrip("=")
    return "SP:0" + text.replace("2", "9")


class Identity:
    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        data = self._read("identity.json", {})
        try:
            self.private = b64u_decode(data["private_key"], 32)
            self.pairing_psk = b64u_decode(data["pairing_psk"], 32)
        except (KeyError, TypeError, ValueError):
            self.private, self.pairing_psk = os.urandom(32), os.urandom(32)
            self._write("identity.json", {"private_key": b64u_encode(self.private),
                                          "pairing_psk": b64u_encode(self.pairing_psk)})
            log.info("created a new Sendspin identity")
        self.public = x25519_public(self.private)
        self.client_id = b64u_encode(self.public)

    # -- files -----------------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.folder, name)

    def _read(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write(self, name, data):
        tmp = self._path(name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds secrets
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self._path(name))

    # -- pairing token ---------------------------------------------------------
    @property
    def token(self):
        return pairing_token(self.public, self.pairing_psk)

    # -- pairing records ---------------------------------------------------------
    def pairings(self):
        records = self._read("pairings.json", [])
        return [r for r in records if isinstance(r, dict) and "server_id" in r and "psk" in r] \
            if isinstance(records, list) else []

    def add_pairing(self, server_id, psk, server_name=None):
        if psk in (SENTINEL_PSK, self.pairing_psk) or len(psk) != 32:
            raise ValueError("unusable long-term PSK")
        with self._lock:
            records = [r for r in self.pairings() if r["server_id"] != server_id]
            records.append({"server_id": server_id, "psk": b64u_encode(psk), "name": server_name or "",
                            "used": int(time.time())})
            records.sort(key=lambda r: r.get("used", 0))
            self._write("pairings.json", records[-MAX_PAIRINGS:])  # oldest dropped first

    def remove_pairing(self, server_id):
        with self._lock:
            records = self.pairings()
            kept = [r for r in records if r["server_id"] != server_id]
            if len(kept) != len(records):
                self._write("pairings.json", kept)
                return True
        return False

    def forget_all(self):
        with self._lock:
            self._write("pairings.json", [])

    def is_paired(self, server_id):
        return any(r["server_id"] == server_id for r in self.pairings())

    def _touch(self, server_id):
        with self._lock:
            records = self.pairings()
            for r in records:
                if r["server_id"] == server_id:
                    r["used"] = int(time.time())
            self._write("pairings.json", records)

    def resolve_psk(self, payload, server_id, allow_sentinel_fallback):
        """Pick the PSK that Noise message 1 names. Returns (psk, category).

        In the first handshake an unknown PSK falls back to the Sentinel: the server then
        sees an unpaired session (for example after it lost its pairing record).
        """
        try:
            msg = json.loads(payload.decode("utf-8"))
            wanted, category = msg["psk_id"], msg["psk_category"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("bad handshake payload") from exc
        if category not in (LONG_TERM, PAIRING, SENTINEL):
            raise ValueError("unknown psk_category %r" % category)
        found = None
        if category == SENTINEL and wanted == SENTINEL_ID:
            found = SENTINEL_PSK
        elif category == PAIRING and wanted == psk_id(self.pairing_psk):
            found = self.pairing_psk
        elif category == LONG_TERM:
            for r in self.pairings():
                try:
                    psk = b64u_decode(r["psk"], 32)
                except ValueError:
                    continue
                if psk_id(psk) == wanted:
                    if r["server_id"] != server_id:
                        raise ValueError("pairing belongs to another server")
                    found = psk
                    self._touch(server_id)
                    break
        if found is not None:
            return found, category
        if allow_sentinel_fallback:
            log.info("server asked for an unknown %s key - continuing unpaired", category)
            return SENTINEL_PSK, SENTINEL
        raise ValueError("server asked for an unknown %s key" % category)
