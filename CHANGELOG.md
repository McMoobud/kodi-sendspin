# Changelog

## 0.1.0 (preview), 2026-10-11
- First working version: Sendspin player/metadata/controller roles over the unencrypted
  transition mode (Music Assistant 2.10 / aiosendspin 9.1.1)
- Pure-Python WebSocket client, clock sync, timestamped PCM pipeline, endless WAV bridge
- Kodi Now Playing metadata + artwork, volume/mute from MA, pause/stop from Kodi
- Automatic sync via Player.getTime() (measured −18 ms vs timeline in tests)
- mDNS auto-discovery, yield-to-video, silent notifications
