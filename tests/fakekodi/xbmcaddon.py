import os
import re
SETTINGS = {"enabled": True, "player_name": "Kodi Sendspin (dev)", "server_url": "", "yield_to_video": True,
            "notifications": True, "lead_ms": 800, "static_delay_ms": 0, "debug": False}
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "service.sendspin"))
class Addon:
    def __init__(self, id=None): pass
    def getAddonInfo(self, key):
        return {"id": "service.sendspin", "path": ROOT, "icon": os.path.join(ROOT, "resources", "icon.png"),
                "profile": "/tmp/fakekodi-profile/", "version": "1.0.0", "name": "Kodi-Sendspin"}[key]
    def getLocalizedString(self, sid):
        po = os.path.join(ROOT, "resources", "language", "resource.language.en_gb", "strings.po")
        text = open(po, encoding="utf-8").read()
        m = re.search(r'msgctxt "#%d"\nmsgid "(.*)"' % sid, text)
        return m.group(1) if m else ""
    def getSettingBool(self, k): return bool(SETTINGS[k])
    def getSettingInt(self, k): return int(SETTINGS[k])
    def getSettingString(self, k): return str(SETTINGS[k])
    def getSetting(self, k): return str(SETTINGS[k])
