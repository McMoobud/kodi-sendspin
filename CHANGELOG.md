# Changelog

## 1.0.1, 2026-10-11
- WebSocket messages are size-limited (a misbehaving server can no longer exhaust memory)
- mDNS discovery ignores malformed replies and waits for the server's address and path
- A discovered server that stays unreachable for 2 minutes is looked up again (new IP after a restart)
- A failed session always closes its socket, so its helper threads stop before a reconnect
- Music never takes over if a video starts while the stream is being prepared
- Repeated Kodi Next/Previous presses are no longer swallowed
- Server URLs like `host/path` get the default port in the right place
- "Connected" notification localised; "No Music Assistant found" shown once, not every minute

## 1.0.0, 2026-10-11
- Named **Kodi-Sendspin**
- Official Sendspin logo for the icon and fanart
- Tested on Fire TV (Kodi 21.2) and Kodi 22 (Piers)

## 0.2.0, 2026-10-11
- Licence changed to GPL-2.0-or-later
- Generic icon and fanart (no third-party logos)
- All user-visible text comes from strings.po
- Logging at debug level only (errors excepted) unless the add-on's debug setting is on
- Developer sync-measurement dump writes only to Kodi's temp folder
- Tested on Fire TV (Kodi 21.2)

## 0.1.0 (preview), 2026-10-11
- First working version: Sendspin player/metadata/controller roles over the unencrypted
  transition mode (Music Assistant 2.10 / aiosendspin 9.1.1)
- Pure-Python WebSocket client, clock sync, timestamped PCM pipeline, endless WAV bridge
- Kodi Now Playing metadata + artwork, volume/mute from MA, pause/stop from Kodi
- Automatic sync via Player.getTime() (measured −18 ms vs timeline in tests)
- mDNS auto-discovery, yield-to-video, silent notifications
