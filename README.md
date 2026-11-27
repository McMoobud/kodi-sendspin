<p align="center"><img src="docs/banner.png" alt="Kodi-Sendspin" width="600"></p>

# Kodi-Sendspin

Turn any Kodi box into a **[Music Assistant](https://music-assistant.io) speaker** using
the **[Sendspin](https://www.sendspin-audio.com) protocol**: synchronised multi-room audio,
with title, artist and artwork on Kodi's own *Now Playing* screen.

![Kodi Now Playing driven by Music Assistant over Sendspin](docs/now-playing.png)

> **Status: v1.1.0.** Speaks the encrypted Sendspin 1.0 protocol (with pairing), and the old unencrypted mode for servers that need it. 1.1.0 was tested against Music Assistant 2.10.13 on LibreELEC (Kodi 21.3, Raspberry Pi). 1.0.x was tested end-to-end against Music Assistant 2.10.4 on Kodi 20.1 (Linux test box), on a Fire TV running Kodi 21.2, and on Kodi 22 (Piers). See [Test results](#test-results) and [Known limitations](#known-limitations).

## Features

- Registers with Music Assistant as a native **Sendspin player** (shows up like any other speaker)
- **Clock-synchronised playback**: audio is released against the server timeline
- **Automatic sync**: measures Kodi's own playback delay and corrects it (≈ ±20 ms in tests)
- **Now Playing** in Kodi: title, artist, album, artwork, live updates on track change
- **Volume / mute from Music Assistant** (applied to the stream; Kodi's own volume untouched)
- **Kodi remote → Music Assistant**: Pause, Stop, Next and Previous control the MA queue
- **Steps aside for video**: while Kodi plays a film it reports itself unavailable, so music never interrupts
- **Zero-config**: finds Music Assistant via mDNS (or set the URL manually)
- **Encrypted** Sendspin 1.0 connection; plays as a guest without pairing, or pair it with a pairing token
- **Pure Python, no dependencies**: works on Android/Fire TV, LibreELEC, Windows, macOS, Linux
- Silent notifications (never plays a UI sound)

## Install

**Easiest:** in Kodi, add **`https://mcmoobud.github.io/kodi-sendspin/`** as a source in
**Settings → File manager → Add source**, then install from it with **Install from zip file** (step 3).
Release zips are also on the [Releases page](https://github.com/McMoobud/kodi-sendspin/releases).

1. Build or download `service.sendspin-<version>.zip`:
   ```bash
   python3 tools/build_zip.py        # -> dist/service.sendspin-1.1.0.zip
   ```
2. In Kodi: **Settings → System → Add-ons → Unknown sources: on**.
3. **Settings → Add-ons → Install from zip file →** pick the zip.
4. It starts immediately and appears in Music Assistant as **Kodi's device name**
   (rename it in the add-on settings).

### Your own repository (automatic updates)

`tools/build_repo.py` builds a small Kodi repository you can host on any web server. Kodi then installs and updates the add-on from it.

1. Copy `repo.example.ini` to `repo.local.ini` (not tracked by git) and set `url` to the address the folder will be served at. Optionally set `publish` to an rsync target.
2. Build it, and optionally upload it with rsync:
   ```bash
   python3 tools/build_repo.py             # -> dist/repo/
   python3 tools/build_repo.py --publish   # build, then rsync to `publish`
   ```
   Without the ini file, pass `--url https://example.com/kodi/` (and `--target user@host:/path/`).
3. In Kodi: **Settings → File manager → Add source**, enter your `url`.
4. **Settings → Add-ons → Install from zip file →** your source → `repository.sendspin-1.0.0.zip`.
5. **Install from repository → Kodi-Sendspin repository → Services → Kodi-Sendspin**.

For a new release, bump the version in `addon.xml`, build and publish again. Kodi picks up the update by itself. Old versions stay on the server unless you pass `--clean`.

### Fire TV notes

- Copy the zip to the Fire TV (for example with *Send Files to TV*, or `adb push service.sendspin-1.1.0.zip /sdcard/Download/`).
- The add-on only runs **while the Kodi app is running**. Android may suspend Kodi when another
  app is in the foreground.
- Your TV/AV receiver adds its own delay that Kodi cannot see. If Kodi sounds slightly behind
  other speakers in a group, raise **Sync → Extra delay** on the *other* players, or lower it here
  (negative values are allowed).

## Music Assistant requirements

- Music Assistant with the Sendspin provider (enabled by default). The encrypted connection needs a server on the Sendspin 1.0 wire: Music Assistant **2.10.13** or later (aiosendspin 10+).
- Older servers: the add-on falls back to the unencrypted *transition mode*. Music Assistant accepts that only while **Sendspin provider → Allow legacy clients** (advanced) is on.

### Pairing

The player works without pairing: Music Assistant lets it play as a **guest**. Pairing ties it to your server with its own key, so a guest connection is no longer needed:

1. In Kodi: **Add-on settings → Security → Show pairing token**. The token starts with `SP:0`. It is also saved as `pairing_token.txt` in the add-on's data folder, which is easier to copy than typing it from the TV.
2. In Music Assistant: open the player's settings → **Pair device → Pairing token**, paste the token.
3. Kodi shows *Paired with …*. From now on it reconnects with that pairing.

To require pairing, turn off **Security → Allow playback without pairing**. **Forget pairings** removes every pairing on the Kodi side.

> Switching to the encrypted connection gives the player a new id, so Music Assistant lists it as a new device. Remove the old one in MA if you like.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Enable Sendspin player | on | |
| Player name | *(Kodi device name)* | Name shown in Music Assistant |
| Server URL | *(auto)* | `ws://host:8927/sendspin`, or just `host` |
| Step aside while Kodi plays video | on | Reports *unavailable* to MA while a video plays |
| Kodi Next/Previous skip in MA | on | Hidden prev/next playlist entries around the live stream |
| Show notifications | on | Always silent |
| Security → Connection | Automatic | Automatic = encrypted, falls back to legacy for servers that need it. Or *Encrypted only* / *Legacy only* |
| Security → Allow playback without pairing | on | Guest playback in Music Assistant without pairing |
| Security → Show pairing token / Forget pairings | | See [Pairing](#pairing) |
| Automatic sync | on | Measures and corrects Kodi's playback delay |
| Extra delay / sync trim | 0 ms | For TV/AVR latency; positive = later |
| Release audio ahead by *(expert)* | 3000 ms | Must exceed Kodi's own buffering (~2.4 s) |
| Debug logging *(expert)* | off | Verbose logs in `kodi.log` |

## How it works

```
Music Assistant ──Sendspin (WebSocket: JSON + timestamped PCM)──▶ add-on (Python service in Kodi)
                                                                   │  clock sync (NTP-style)
                                                                   │  jitter buffer + timeline
                                                                   ▼
                                  http://127.0.0.1:<port>/stream/<n>.wav  (endless WAV, released
                                                                   │       just ahead of play time)
                                                                   ▼
                                                     Kodi's own player (PAPlayer) → speakers
```

- **`resources/lib/sendspin/websocket.py`**: minimal RFC 6455 client (stdlib only).
- **`client.py`**: Sendspin roles `player@v1`, `metadata@v1`, `controller@v1`; encrypted or legacy wire; hello/activate/state/time/commands; pairing; reconnect with back-off; watchdog.
- **`noise.py`**: Noise `KKpsk2_25519_ChaChaPoly_SHA256` as the responder (the server always starts the handshake), encrypted framing and fragments, re-handshakes.
- **`crypto.py`**: X25519 and ChaCha20-Poly1305 in pure Python. Uses `cryptography` or OpenSSL's libcrypto (via `ctypes`) when they are there; every backend is checked against the RFC test vector first.
- **`identity.py`**: the player's keypair (its public key is the client id), the pairing key behind the pairing token, and the pairing records. Stored in the add-on's data folder.
- **`clock.py`**: the spec's time filter (a Kalman filter tracking offset and drift), fed with the best of each burst of 8 time probes.
- **`audio.py`**: timestamped chunks → continuous timeline (gap fill, late drop, overlap trim), software volume, and a *broadcaster* that fans the released audio out to every HTTP reader. Kodi opens the stream several times (probe, tag reader, player).
- **`kodi_bridge.py`**: drives `xbmc.Player` from a worker thread, Now Playing tags, video yield, Kodi→MA pause/stop, **auto-sync** (`Player.getTime()` mapped onto the released-audio index).
- **`discovery.py`**: mDNS browse for `_sendspin-server._tcp`.

### Kodi gotchas this design avoids

- `Player.play()` / `stop()` **block** while Kodi opens or closes the stream. Calling them from the
  WebSocket thread stalls it, and Music Assistant drops the connection (*No PONG*). All Kodi calls run on
  a worker thread.
- If our HTTP stream is still open, those calls appear to hold the Python interpreter lock while
  PAPlayer waits for the stream to close. Closing the stream needs that lock, so the two **deadlock**
  until curl's timeout. The add-on never calls them while a reader is connected, and ends streams by
  closing them, so Kodi stops by itself.
- Ended streams answer **404**, so curl's automatic reconnect gives up straight away.

## Test results

Run against a live Music Assistant 2.10.4 (aiosendspin 9.1.1):

| Test | Result |
|---|---|
| Unit tests (`python3 -m unittest discover -s tests`) | ✅ 41/41 (1.1.0: crypto vectors, pairing token, framing, scripted encrypted session with pairing) |
| **1.1.0 interop** against a real aiosendspin 11.0.0 server (`tests/interop_server.py`) | ✅ encrypted guest playback, legacy playback, token pairing then paired reconnect; real-time PCM, no errors |
| **1.1.0 against Music Assistant 2.10.13** (HA add-on) | ✅ encrypted guest playback without pairing or "Allow legacy clients": 99.5 s headless, one connection, no errors |
| **1.1.0 on LibreELEC, Kodi 21.3 (Raspberry Pi, HDMI)** | ✅ installed from a `build_repo.py` repository, found MA by mDNS across subnets, plays; auto-sync corrected Kodi from −653 ms to −6…+3 ms |
| Headless client (`tests/harness.py`): 80 s radio | ✅ 0.992× real-time, 0 late drops, real audio (RMS 5939) |
| Fake-Kodi end-to-end (`tests/e2e_service.py`) | ✅ play, MA volume, yield to video, Kodi stop → MA stop |
| Real Kodi: remote **Next/Previous** | ✅ one press = one MA skip (Kodi probes playlist items twice; de-duplicated) |
| Real Kodi: **MA group** with another Sendspin player | ✅ both play; Kodi −21 ms vs the shared timeline |
| Real Kodi: **10-min soak** | ✅ 0 disconnects/timeouts/errors; 1 small auto-sync correction after ~8 min |
| **Real Kodi 20.1** (Linux container, silent null sink) | ✅ play, MA next ×2 with title updates, pause in Kodi → MA pauses, MA play → resumes, MA volume, MA stop → Kodi stops; 0 errors/resets |
| **Fire TV, Kodi 21.2 (Android)** | ✅ hands-on test at home |
| **Kodi 22 (Piers)** | ✅ hands-on test at home |
| `kodi-addon-checker --branch piers` | ✅ no problems, no warnings |
| **Sync** (`tools/measure_sync.py`: capture Kodi's actual output and cross-correlate with the timeline) | Without auto-sync: Kodi −565 ms (early) with the 3 s lead, or **+1652 ms** (late) with a 0.8 s lead. **With auto-sync: −18 ms, jitter 1 ms** |

## Known limitations / roadmap

- **Decryption cost on slow devices.** Without a native backend, pure-Python ChaCha20-Poly1305 does about 4 MB/s on a desktop CPU; PCM needs 0.19 MB/s. On a Fire TV the native libcrypto backend should normally be used, but this has not been measured there yet.
- **Pairing token only.** The code-based pairing methods (CPace) are not implemented. The spec only requires the token method.
- The add-on dials the server (client-initiated). It does not advertise itself for server-initiated connections.
- **PCM only** (16-bit, 44.1/48 kHz). FLAC would save bandwidth on Wi-Fi.
- TV/AV receiver delay is invisible to Kodi and needs the manual trim (**Sync → Extra delay**).
- Starting or skipping takes about 3 s (the release lead), because Kodi needs ~2.4 s of buffer before it plays.
- Kodi's *Previous* restarts the current item if pressed more than ~3 s in (Kodi behaviour), so press it twice or use MA.
- Music Assistant 2.10 rejects `set_static_delay` in the hello, so delay changes from MA's UI aren't advertised (legacy wire only).

## Development

```bash
python3 -m unittest discover -s tests -p "test_*.py"     # unit tests
python3 tests/harness.py ws://<ma-host>:8927/sendspin 60   # headless player, writes /tmp/sendspin_test.wav
HA_URL=http://<ha>:8123 HA_TOKEN=... HA_ENTITY=media_player.<player> python3 tests/e2e_service.py   # fake-Kodi E2E via HA
python3 tools/build_zip.py                                  # installable zip
python3 tools/build_repo.py --url https://example.com/kodi/  # Kodi repository in dist/repo/
```

Interop against a real Sendspin server, using `aiosendspin` in a local venv (`python3 -m venv .venv && .venv/bin/pip install "aiosendspin[server]"`):

```bash
.venv/bin/python tests/interop_server.py --seconds 30 [--legacy] [--pair-token SP:0...]
.venv/bin/python tests/interop_client.py --identity /path/to/scratch-id --mode auto --seconds 20
```

`interop_client.py --print-token` prints the pairing token for `--pair-token`.

`tools/start-kodi-test.sh`, `tools/deploy_test_lxc.sh` and `tools/measure_sync.py` set up a **silent**
real-Kodi test box (Xvfb + PulseAudio null sink). They let you test against real Kodi without
making any sound.

## License

[GPL-2.0-or-later](LICENSE). This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 2 of the License, or (at your option) any later version.

Kodi-Sendspin is an unofficial community add-on. Sendspin is a project of the
[Open Home Foundation](https://www.openhomefoundation.org). Kodi is a trademark of the XBMC Foundation.

---

## Contributors

- McMoobud, author
- Matthieu Lalonde, encrypted Sendspin and self-hosted repository
- Claude (Anthropic), AI pair programmer

<sub>Disclaimer: this add-on was written with the help of Claude, an AI model by Anthropic. It has been tested as described above, but it is a community preview. Use it at your own risk.</sub>
