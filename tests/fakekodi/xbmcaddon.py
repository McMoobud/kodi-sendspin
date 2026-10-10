import os
SETTINGS = {"enabled": True, "player_name": "Kodi Sendspin (dev)", "server_url": "", "yield_to_video": True,
            "notifications": True, "lead_ms": 800, "static_delay_ms": 0, "debug": False}
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "service.sendspin"))
class Addon:
    def __init__(self, id=None): pass
    def getAddonInfo(self, key):
        return {"id": "service.sendspin", "path": ROOT, "icon": os.path.join(ROOT, "resources", "icon.png"),
                "profile": "/tmp/fakekodi-profile/", "version": "0.1.0", "name": "Sendspin Player"}[key]
    def getSettingBool(self, k): return bool(SETTINGS[k])
    def getSettingInt(self, k): return int(SETTINGS[k])
    def getSettingString(self, k): return str(SETTINGS[k])
    def getSetting(self, k): return str(SETTINGS[k])
