"""End-to-end test of service.sendspin with fake Kodi modules + a real Music Assistant.

Drives the real MA through Home Assistant (token from env HA_URL / HA_TOKEN),
plays a radio stream ONLY to the dev player, and checks the add-on reacts:
playback start, metadata on the Now Playing tag, volume from MA, stepping aside
for video, and the user pressing Stop in Kodi. Nothing is audible anywhere.
"""

import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakekodi"))
sys.path.insert(0, os.path.join(HERE, "..", "service.sendspin", "resources", "lib"))

import xbmc  # noqa: E402  (fake)

HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
ENTITY = os.environ["HA_ENTITY"]  # MA player entity for the add-on, e.g. media_player.kodi_test
STREAM = os.environ.get("TEST_STREAM", "https://ice1.somafm.com/groovesalad-128-mp3")


def ha(method, path, body=None):
    req = urllib.request.Request(HA_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + HA_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read().decode()
        return json.loads(t) if t else None


def state():
    s = ha("GET", "/api/states/" + ENTITY)
    a = s["attributes"]
    return s["state"], a.get("volume_level"), a.get("media_title")


def step(label):
    print("\n##### %s  (t=%.0fs)  kodi=%s  stats=%s" % (label, time.time() - T0, xbmc.Player._state, xbmc.Player.stats), flush=True)


os.makedirs("/tmp/fakekodi-profile", exist_ok=True)
with open("/tmp/fakekodi-profile/client_id", "w") as f:
    f.write("kodi-sendspin-dev")

import service_main as service  # noqa: E402

T0 = time.time()
import faulthandler; faulthandler.dump_traceback_later(int(os.environ.get("DUMP_AT", "0")) or 10**6)
threading.Thread(target=service.run, daemon=True).start()
results = {}

time.sleep(8)
step("start radio on dev player")
ha("POST", "/api/services/music_assistant/play_media",
   {"entity_id": ENTITY, "media_id": STREAM, "media_type": "radio", "enqueue": "replace"})
time.sleep(20)
step("after 20s of playback")
results["bytes_after_20s"] = xbmc.Player.stats["bytes"]
results["ha_state_playing"] = state()

ha("POST", "/api/services/media_player/volume_set", {"entity_id": ENTITY, "volume_level": 0.35})
time.sleep(4)
step("after volume_set 0.35")
results["ha_after_volume"] = state()

step("simulate user starting a video in Kodi")
xbmc.Player.fake_video(True)
time.sleep(5)
results["ha_while_video"] = state()
xbmc.Player.fake_video(False)
time.sleep(3)

step("restart radio, then user presses STOP in Kodi")
ha("POST", "/api/services/music_assistant/play_media",
   {"entity_id": ENTITY, "media_id": STREAM, "media_type": "radio", "enqueue": "replace"})
time.sleep(20)  # the add-on ignores stops within 4 s of its own play()
results["ha_before_user_stop"] = state()
xbmc.Player().stop()
time.sleep(6)
results["ha_after_user_stop"] = state()
step("done")
xbmc.ABORT.set()
time.sleep(3)

print("\n===== RESULTS =====")
for k, v in results.items():
    print("%-22s %s" % (k, v))
print("\n===== KODI EVENTS =====")
for e in xbmc.EVENTS:
    s = str(e)
    print(" ", s[:260])
