"""Fake xbmc module: enough behaviour to run service.sendspin headless."""
import threading, time, urllib.request, os
LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR, LOGFATAL = 0, 1, 2, 3, 4
EVENTS = []
ABORT = threading.Event()
def log(msg, level=LOGINFO):
    print("KODI-LOG[%d] %s" % (level, msg), flush=True)
def getInfoLabel(label):
    return {"System.FriendlyName": "FakeKodi", "System.BuildVersion": "21.2 (21.2.0) Git:fake"}.get(label, "")
class Monitor:
    def abortRequested(self): return ABORT.is_set()
    def waitForAbort(self, t=0):
        return ABORT.wait(t)
class Player:
    _state = {"file": None, "playing": False, "paused": False, "video": False, "gen": 0}
    _instances = []
    stats = {"bytes": 0, "plays": 0, "stops": 0, "infotag_updates": 0}
    def __init__(self):
        Player._instances.append(self)
    def _fire(self, name):
        for p in Player._instances:
            fn = getattr(p, name, None)
            if fn: fn()
    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        st = Player._state
        st["gen"] += 1; gen = st["gen"]
        st.update(file=item, playing=True, paused=False, video=False)
        Player.stats["plays"] += 1
        EVENTS.append(("play", item, listitem and listitem.info))
        threading.Thread(target=self._consume, args=(item, gen), daemon=True).start()
    def _consume(self, url, gen):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                while Player._state["gen"] == gen and Player._state["playing"]:
                    b = r.read(8192)
                    if not b: break
                    Player.stats["bytes"] += len(b)
        except Exception as e:
            EVENTS.append(("consume_error", str(e)))
        if Player._state["gen"] == gen and Player._state["playing"]:
            Player._state.update(playing=False, file=None)
            self._fire("onPlayBackEnded")
    def stop(self):
        st = Player._state
        if st["playing"]:
            st.update(playing=False, file=None); Player.stats["stops"] += 1
            EVENTS.append(("stop",))
            self._fire("onPlayBackStopped")
    def pause(self):
        st = Player._state; st["paused"] = not st["paused"]
        self._fire("onPlayBackPaused" if st["paused"] else "onPlayBackResumed")
    def isPlaying(self): return Player._state["playing"]
    def isPlayingVideo(self): return Player._state["playing"] and Player._state["video"]
    def getPlayingFile(self):
        if not Player._state["playing"]: raise RuntimeError("Kodi is not playing any file")
        return Player._state["file"]
    def updateInfoTag(self, item):
        Player.stats["infotag_updates"] += 1
        EVENTS.append(("infotag", item.info))
    # helper for tests: simulate user starting a video
    @classmethod
    def fake_video(cls, on):
        cls._state.update(playing=on, video=on, file="smb://films/film.mkv" if on else None, gen=cls._state["gen"] + 1)
