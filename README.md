<p align="center"><img src="docs/sendspin-logo.png" alt="Sendspin" width="434"></p>

# Kodi-Sendspin

Turn any Kodi box into a **[Music Assistant](https://music-assistant.io) speaker** using
the **[Sendspin](https://www.sendspin-audio.com) protocol**: synchronised multi-room audio,
with title, artist and artwork on Kodi's own *Now Playing* screen.

![Kodi Now Playing driven by Music Assistant over Sendspin](docs/now-playing.png)

> **Status: v1.0.1.** It works end-to-end against Music Assistant 2.10.4
> on Kodi 20.1 (Linux test box), on a Fire TV running Kodi 21.2, and on Kodi 22 (Piers). See
> [Test results](#test-results) and [Known limitations](#known-limitations).

## Features

- Registers with Music Assistant as a native **Sendspin player** (shows up like any other speaker)
- **Clock-synchronised playback**: audio is released against the server timeline
- **Automatic sync**: measures Kodi's own playback delay and corrects it (≈ ±20 ms in tests)
- **Now Playing** in Kodi: title, artist, album, artwork, live updates on track change
- **Volume / mute from Music Assistant** (applied to the stream; Kodi's own volume untouched)
- **Kodi remote → Music Assistant**: Pause, Stop, Next and Previous control the MA queue
- **Steps aside for video**: while Kodi plays a film it reports itself unavailable, so music never interrupts
- **Zero-config**: finds Music Assistant via mDNS (or set the URL manually)
- **Pure Python, no dependencies**: works on Android/Fire TV, LibreELEC, Windows, macOS, Linux
- Silent notifications (never plays a UI sound)

## Install

**Easiest:** in Kodi, add **`https://mcmoobud.github.io/kodi-sendspin/`** as a source in
**Settings → File manager → Add source**, then install from it with **Install from zip file** (step 3).
Release zips are also on the [Releases page](https://github.com/McMoobud/kodi-sendspin/releases).

1. Build or download `service.sendspin-<version>.zip`:
   ```bash
   python3 tools/build_zip.py        # -> dist/service.sendspin-1.0.1.zip
   ```
2. In Kodi: **Settings → System → Add-ons → Unknown sources: on**.
3. **Settings → Add-ons → Install from zip file →** pick the zip.
4. It starts immediately and appears in Music Assistant as **Kodi's device name**
   (rename it in the add-on settings).

### Fire TV notes

- Copy the zip to the Fire TV (for example with *Send Files to TV*, or `adb push service.sendspin-1.0.1.zip /sdcard/Download/`).
- The add-on only runs **while the Kodi app is running**. Android may suspend Kodi when another
  app is in the foreground.
- Your TV/AV receiver adds its own delay that Kodi cannot see. If Kodi sounds slightly behind
  other speakers in a group, raise **Sync → Extra delay** on the *other* players, or lower it here
  (negative values are allowed).

## Music Assistant requirements

- Music Assistant **2.10+** with the Sendspin provider (enabled by default).
- **"Allow legacy clients"** must stay enabled in the Sendspin provider settings (it is by default).
  This add-on uses Sendspin's *unencrypted transition mode*. See [limitations](#known-limitations).

## Settings

| Setting | Default | Notes |
|---|---|---|
| Enable Sendspin player | on | |
| Player name | *(Kodi device name)* | Name shown in Music Assistant |
| Server URL | *(auto)* | `ws://host:8927/sendspin`, or just `host` |
| Step aside while Kodi plays video | on | Reports *unavailable* to MA while a video plays |
| Kodi Next/Previous skip in MA | on | Hidden prev/next playlist entries around the live stream |
| Show notifications | on | Always silent |
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
- **`client.py`**: Sendspin roles `player@v1`, `metadata@v1`, `controller@v1`; hello/state/time/commands; reconnect with back-off; watchdog.
- **`clock.py`**: server↔local offset from the lowest-RTT time probes, smoothed.
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
| Unit tests (`python3 -m unittest discover -s tests`) | ✅ 16/16 |
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

- **Unencrypted transition mode only.** Sendspin 1.0 uses Noise (X25519 + ChaCha20-Poly1305).
  That's feasible in pure Python for the handshake, but probably too slow for PCM on a Fire TV CPU.
  Options: AES-GCM via Android's `javax.crypto`? Not reachable from Kodi Python. Or request a
  compressed codec (FLAC/Opus) to cut bytes, then decrypt.
- **PCM only** (16-bit, 44.1/48 kHz). FLAC would save bandwidth on Wi-Fi.
- TV/AV receiver delay is invisible to Kodi and needs the manual trim (**Sync → Extra delay**).
- Starting or skipping takes about 3 s (the release lead), because Kodi needs ~2.4 s of buffer before it plays.
- Kodi's *Previous* restarts the current item if pressed more than ~3 s in (Kodi behaviour), so press it twice or use MA.
- Music Assistant 2.10 rejects `set_static_delay` in the hello, so delay changes from MA's UI aren't advertised.

## Development

```bash
python3 -m unittest discover -s tests -p "test_*.py"     # unit tests
python3 tests/harness.py ws://<ma-host>:8927/sendspin 60   # headless player, writes /tmp/sendspin_test.wav
HA_URL=http://<ha>:8123 HA_TOKEN=... HA_ENTITY=media_player.<player> python3 tests/e2e_service.py   # fake-Kodi E2E via HA
python3 tools/build_zip.py                                  # installable zip
```

`tools/start-kodi-test.sh`, `tools/deploy_test_lxc.sh` and `tools/measure_sync.py` set up a **silent**
real-Kodi test box (Xvfb + PulseAudio null sink). They let you test against real Kodi without
making any sound.

## License

[GPL-2.0-or-later](LICENSE). This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 2 of the License, or (at your option) any later version.

Kodi-Sendspin is an unofficial community add-on. The Sendspin name and logo belong to the
[Open Home Foundation](https://www.openhomefoundation.org); the logo is taken from
[sendspin-audio.com](https://www.sendspin-audio.com).

---

<sub>Disclaimer: this add-on was written with the help of Claude, an AI model by Anthropic. It has been tested as described above, but it is a community preview. Use it at your own risk.</sub>
