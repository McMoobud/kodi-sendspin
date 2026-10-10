import xbmc
class _Tag:
    def __init__(self, info): self.info = info
    def __getattr__(self, name):
        if name.startswith("set"):
            key = name[3:].lower()
            def setter(v, _k=key): self.info[_k] = v
            return setter
        raise AttributeError(name)
class ListItem:
    def __init__(self, label="", label2="", path="", offscreen=False):
        self.info = {"label": label, "path": path}
    def getMusicInfoTag(self): return _Tag(self.info)
    def setArt(self, art): self.info["art"] = art
    def setMimeType(self, m): self.info["mime"] = m
    def setContentLookup(self, b): self.info["contentlookup"] = b
    def setProperty(self, k, v): self.info[k] = v
class Dialog:
    def notification(self, heading, message, icon="", time=5000, sound=True):
        xbmc.EVENTS.append(("notification", heading, message, "SOUND!" if sound else "silent"))
