"""Glue between the Sendspin client and Kodi's player / GUI."""

import logging
import os
import socket
import threading
import uuid

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from sendspin import __version__
from sendspin.audio import AudioPipeline, WavStreamServer
from sendspin.client import Listener, SendspinClient
from sendspin.clock import ClockSync
from sendspin.discovery import discover

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ICON = ADDON.getAddonInfo("icon")
log = logging.getLogger("sendspin.kodi")


class KodiLogHandler(logging.Handler):
    """Route Python logging into kodi.log."""

    LEVELS = {logging.DEBUG: xbmc.LOGDEBUG, logging.INFO: xbmc.LOGINFO,
              logging.WARNING: xbmc.LOGWARNING, logging.ERROR: xbmc.LOGERROR,
              logging.CRITICAL: xbmc.LOGFATAL}

    def __init__(self, debug):
        super().__init__()
        self.debug = debug

    def emit(self, record):
        level = self.LEVELS.get(record.levelno, xbmc.LOGINFO)
        if self.debug and level == xbmc.LOGDEBUG:
            level = xbmc.LOGINFO  # make debug visible without Kodi's global debug mode
        xbmc.log("[%s] %s: %s" % (ADDON_ID, record.name, self.format(record)), level)


def setup_logging(debug):
    root = logging.getLogger("sendspin")
    root.handlers[:] = [KodiLogHandler(debug)]
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False


def _setting_str(key):
    try:
        return ADDON.getSettingString(key).strip()
    except Exception:  # noqa: BLE001 - very old Kodi
        return (ADDON.getSetting(key) or "").strip()


def _setting_bool(key, default):
    try:
        return ADDON.getSettingBool(key)
    except Exception:  # noqa: BLE001
        return default


def _setting_int(key, default):
    try:
        return ADDON.getSettingInt(key)
    except Exception:  # noqa: BLE001
        return default


def client_id():
    """Stable per-installation id, stored in the add-on profile folder."""
    profile = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    os.makedirs(profile, exist_ok=True)
    path = os.path.join(profile, "client_id")
    try:
        with open(path) as f:
            cid = f.read().strip()
            if cid:
                return cid
    except OSError:
        pass
    cid = "kodi-" + uuid.uuid4().hex[:12]
    with open(path, "w") as f:
        f.write(cid)
    return cid


def device_name():
    name = _setting_str("player_name")
    if name:
        return name
    friendly = xbmc.getInfoLabel("System.FriendlyName")
    if friendly and "System.FriendlyName" not in friendly:
        return friendly
    return "Kodi (%s)" % socket.gethostname()


def notify(message, ms=4000):
    if _setting_bool("notifications", True):
        # sound=False: never make a noise, even for toasts
        xbmcgui.Dialog().notification("Sendspin", message, ICON, ms, False)


class SendspinPlayer(xbmc.Player):
    """Kodi player that knows which item is ours and reports user actions."""

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    def onPlayBackPaused(self):  # noqa: N802
        if self.bridge.is_our_item():
            self.bridge.user_action("pause")

    def onPlayBackResumed(self):  # noqa: N802
        if self.bridge.is_our_item():
            self.bridge.user_action("play")

    def onPlayBackStopped(self):  # noqa: N802
        self.bridge.on_kodi_stopped()

    def onPlayBackEnded(self):  # noqa: N802
        self.bridge.on_kodi_stopped()


class Bridge(Listener):
    """Owns pipeline + HTTP server + Sendspin client and drives Kodi."""

    def __init__(self):
        self.clock = ClockSync()
        self.pipeline = AudioPipeline(
            self.clock,
            lead_ms=_setting_int("lead_ms", 800),
            static_delay_ms=_setting_int("static_delay_ms", 0),
        )
        self.http = WavStreamServer(self.pipeline)
        self.player = SendspinPlayer(self)
        self.client = None
        self.url_prefix = None
        self._current_url = None
        self._expect_stop = False
        self._lock = threading.Lock()
        self.metadata = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self, server_url):
        self.http.start()
        self.url_prefix = "http://127.0.0.1:%d/stream/" % self.http.port
        self.client = SendspinClient(
            server_url, client_id(), device_name(), self.pipeline, self,
            version=__version__, product="Kodi %s" % xbmc.getInfoLabel("System.BuildVersion").split(" ")[0],
        )
        log.info("starting: name=%r id=%s url=%s http=%s", self.client.name, self.client.client_id,
                 server_url, self.url_prefix)
        self.client.start()

    def stop(self):
        if self.client:
            self.client.stop()
        self._stop_kodi_player()
        self.http.stop()

    # -- helpers -----------------------------------------------------------
    def is_our_item(self):
        try:
            return self.player.isPlaying() and self.player.getPlayingFile().startswith(self.url_prefix or "\x00")
        except RuntimeError:
            return False

    def other_media_playing(self):
        try:
            return self.player.isPlaying() and not self.is_our_item()
        except RuntimeError:
            return False

    def _listitem(self, url):
        md = self.metadata
        title = md.get("title") or "Music Assistant"
        li = xbmcgui.ListItem(label=title, path=url, offscreen=True)
        li.setMimeType("audio/wav")
        li.setContentLookup(False)
        tag = li.getMusicInfoTag()
        tag.setTitle(title)
        if md.get("artist"):
            tag.setArtist(md["artist"])
        if md.get("album"):
            tag.setAlbum(md["album"])
        if md.get("album_artist"):
            tag.setAlbumArtist(md["album_artist"])
        if md.get("year"):
            tag.setYear(int(md["year"]))
        if md.get("track"):
            tag.setTrack(int(md["track"]))
        progress = md.get("progress") or {}
        if progress.get("track_duration"):
            tag.setDuration(int(progress["track_duration"] // 1000))
        art = md.get("artwork_url")
        if art:
            li.setArt({"thumb": art, "icon": art, "fanart": art, "album.thumb": art})
        tag.setComment("Streaming from %s via Sendspin" % (self.client.server_name if self.client else "Music Assistant"))
        return li

    def _play_generation(self, generation):
        if _setting_bool("yield_to_video", True) and self.other_media_playing():
            log.info("Kodi is busy with other media - not taking over playback")
            return
        url = self.http.url(generation)
        with self._lock:
            self._current_url = url
            self._expect_stop = True  # play() stops the previous item first
        log.info("Kodi play %s", url)
        self.player.play(url, self._listitem(url))

    def _stop_kodi_player(self):
        if self.is_our_item():
            with self._lock:
                self._expect_stop = True
            self.player.stop()

    # -- Sendspin events (client thread) ------------------------------------
    def on_connected(self, server_name):
        notify("Connected to %s" % server_name)

    def on_disconnected(self):
        self._stop_kodi_player()

    def on_stream_start(self, fmt, generation):
        self._play_generation(generation)

    def on_stream_clear(self, generation):
        # Seek/skip: Kodi's own buffer holds stale audio, so restart the item.
        if self.is_our_item() or self._current_url:
            self._play_generation(generation)

    def on_stream_end(self):
        self._stop_kodi_player()
        with self._lock:
            self._current_url = None

    def on_metadata(self, metadata):
        self.metadata = metadata
        if self.is_our_item():
            try:
                self.player.updateInfoTag(self._listitem(self.player.getPlayingFile()))
            except (RuntimeError, AttributeError) as exc:  # updateInfoTag needs Kodi 20+
                log.debug("updateInfoTag failed: %s", exc)

    def on_volume(self, volume, muted):
        log.info("volume=%s muted=%s", volume, muted)

    # -- Kodi events ----------------------------------------------------------
    def user_action(self, command):
        if self.client:
            self.client.send_controller(command)

    def on_kodi_stopped(self):
        with self._lock:
            expected = self._expect_stop
            self._expect_stop = False
            ours = self._current_url is not None
        if ours and not expected and self.pipeline.active:
            log.info("user stopped playback in Kodi - asking Music Assistant to stop")
            self.user_action("stop")
            with self._lock:
                self._current_url = None

    def tick(self):
        """Called ~1/s from the service loop."""
        if self.client is None:
            return
        busy = _setting_bool("yield_to_video", True) and self.other_media_playing()
        self.client.set_available(not busy)
        # play() fires onPlayBackStopped for the previous item asynchronously; once our
        # item is up, later stops are user actions again.
        if self.is_our_item():
            with self._lock:
                self._expect_stop = False


def resolve_server_url():
    url = _setting_str("server_url")
    if url:
        if not url.startswith("ws://"):
            url = "ws://" + url
        if url.count(":") == 1:  # no port given
            url += ":8927"
        if "/" not in url[5:]:
            url += "/sendspin"
        return url
    servers = discover()
    if servers:
        log.info("discovered %s", servers)
        return servers[0]["url"]
    return None
